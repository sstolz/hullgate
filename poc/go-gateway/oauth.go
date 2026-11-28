package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/ory/fosite"
	"github.com/ory/fosite/compose"
	"github.com/ory/fosite/handler/oauth2"
	"github.com/ory/fosite/storage"
	"github.com/ory/fosite/token/jwt"
)

// dynClient : client pré-enregistré dont l'ensemble d'audiences admises suit les rôles existants.
type dynClient struct {
	*fosite.DefaultClient
	s *Server
}

func (c *dynClient) GetAudience() fosite.Arguments {
	c.s.mu.RLock()
	defer c.s.mu.RUnlock()
	var out fosite.Arguments
	for _, r := range c.s.roles {
		out = append(out, c.s.resourceURL(r.Path))
	}
	return out
}

func (s *Server) initOAuth() {
	cfg := &fosite.Config{
		AccessTokenLifespan:   s.accessTTL,
		RefreshTokenLifespan:  30 * 24 * time.Hour,
		AuthorizeCodeLifespan: 2 * time.Minute,
		AccessTokenIssuer:     s.base,
		GlobalSecret:          func() []byte { b := make([]byte, 32); rand.Read(b); return b }(),
		EnforcePKCE:           true,
		RefreshTokenScopes:    []string{},
		TokenURL:              s.base + "/token",
	}
	s.store = storage.NewMemoryStore()
	rs := &revStore{MemoryStore: s.store, s: s}
	mk := func(id string, redirects []string) {
		s.store.Clients[id] = &dynClient{s: s, DefaultClient: &fosite.DefaultClient{
			ID: id, Public: true, RedirectURIs: redirects,
			GrantTypes: []string{"authorization_code", "refresh_token"}, ResponseTypes: []string{"code"},
			Scopes: []string{"mcp", "offline_access"}}}
	}
	mk("bench-gw-client", []string{"http://localhost/callback"})
	mk(env("OPERATOR_CLIENT_ID", "claude-operator"), strings.Split(env("OPERATOR_REDIRECTS",
		"https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback"), ","))
	getKey := func(context.Context) (interface{}, error) { return s.key, nil }
	strat := &compose.CommonStrategy{
		CoreStrategy: compose.NewOAuth2JWTStrategy(getKey, compose.NewOAuth2HMACStrategy(cfg), cfg),
		Signer:       &jwt.DefaultSigner{GetPrivateKey: getKey},
	}
	s.provider = compose.Compose(cfg, rs, strat,
		compose.OAuth2AuthorizeExplicitFactory,
		compose.OAuth2RefreshTokenGrantFactory,
		compose.OAuth2TokenRevocationFactory,
		compose.OAuth2PKCEFactory)
}

// revStore : la révocation fosite (point /revoke, rotation du jeton de renouvellement, réemploi détecté) révoque aussi,
// dans le coffre, les jti des jetons d'accès JWT émis pour la même demande ; sans lui, un jeton d'accès JWT révoqué
// resterait accepté par le point d'accès, qui ne consulte pas le stockage de fosite (jetons sans état).
type revStore struct {
	*storage.MemoryStore
	s *Server
}

// revokeReq : erreur rendue, jamais avalée (refus par défaut). À /token, elle fait échouer la rotation (server_error,
// aucun jeton délivré). À /revoke, fosite v0.49.0 la traduit en ErrTemporarilyUnavailable mais WriteRevocationResponse
// écrit 200 pour toute erreur hors invalid_request / invalid_client (revoke_handler.go:118-121) : le gestionnaire /revoke
// la relit donc dans un porteur du contexte (revokeFailure) et écrit lui-même le 503 (RFC 7009 §2.2.1).
func (s *Server) revokeReq(requestID string) error {
	if s.storeDown.Load() {
		return errStore
	}
	_, err := s.db.Exec(`insert or ignore into revoked(jti) select jti from issued where req=?`, requestID)
	return err
}

// revokeFailure : porteur posé dans le contexte par le gestionnaire /revoke ; revStore y dépose l'échec de persistance.
type revokeFailure struct{ err error }

type revokeFailureKey struct{}

func noteRevokeFailure(ctx context.Context, err error) error {
	if f, ok := ctx.Value(revokeFailureKey{}).(*revokeFailure); ok && f.err == nil {
		f.err = err
	}
	return err
}

func (r *revStore) RevokeAccessToken(ctx context.Context, requestID string) error {
	if err := r.s.revokeReq(requestID); err != nil {
		return noteRevokeFailure(ctx, err)
	}
	return r.MemoryStore.RevokeAccessToken(ctx, requestID)
}

// RotateRefreshToken : MemoryStore révoque l'ancien jeton d'accès par un appel interne qui ne passe pas par revStore.
func (r *revStore) RotateRefreshToken(ctx context.Context, requestID string, sig string) error {
	if err := r.s.revokeReq(requestID); err != nil {
		return err
	}
	return r.MemoryStore.RotateRefreshToken(ctx, requestID, sig)
}

func (r *revStore) RevokeRefreshToken(ctx context.Context, requestID string) error {
	if err := r.s.revokeReq(requestID); err != nil {
		return noteRevokeFailure(ctx, err)
	}
	return r.MemoryStore.RevokeRefreshToken(ctx, requestID)
}

func (s *Server) routesOAuth(mux *http.ServeMux) {
	mux.HandleFunc("GET /.well-known/oauth-authorization-server", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{
			"issuer": s.base, "authorization_endpoint": s.base + "/authorize", "token_endpoint": s.base + "/token",
			"revocation_endpoint": s.base + "/revoke", "response_types_supported": []string{"code"},
			"grant_types_supported":                  []string{"authorization_code", "refresh_token"},
			"code_challenge_methods_supported":       []string{"S256"},
			"token_endpoint_auth_methods_supported":  []string{"none"},
			"scopes_supported":                       []string{"mcp", "offline_access"},
			"authorization_response_iss_parameter_supported": false})
	})
	// métadonnées de ressource propres à chaque point d'accès (RFC 9728) ; aucune racine globale (T14) : la racine répond 404
	// (sans ce motif exact, ServeMux redirigerait en 301 vers la racine du sous-arbre)
	mux.HandleFunc("GET /.well-known/oauth-protected-resource", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 404, map[string]string{"error": "not_found"})
	})
	mux.HandleFunc("GET /.well-known/oauth-protected-resource/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/.well-known/oauth-protected-resource")
		role := s.roleByPath(path)
		if role == nil {
			writeJSON(w, 404, map[string]string{"error": "not_found"})
			return
		}
		writeJSON(w, 200, map[string]any{"resource": s.resourceURL(path), "authorization_servers": []string{s.base},
			"bearer_methods_supported": []string{"header"}, "scopes_supported": []string{"mcp"}})
	})
	mux.HandleFunc("/authorize", s.authorize)
	mux.HandleFunc("POST /token", s.token)
	// /revoke refusé par défaut : coffre en panne ou révocation non persistée → 503 temporarily_unavailable, écrit ici
	// (fosite répondrait 200, cf. revokeReq) ; le chemin nominal reste celui de fosite.
	mux.HandleFunc("POST /revoke", func(w http.ResponseWriter, r *http.Request) {
		if s.storeDown.Load() {
			writeJSON(w, 503, map[string]string{"error": "temporarily_unavailable"})
			return
		}
		fail := &revokeFailure{}
		ctx := context.WithValue(r.Context(), revokeFailureKey{}, fail)
		err := s.provider.NewRevocationRequest(ctx, r)
		if fail.err != nil {
			log.Printf("revoke: révocation non persistée, 503")
			writeJSON(w, 503, map[string]string{"error": "temporarily_unavailable"})
			return
		}
		s.provider.WriteRevocationResponse(ctx, w, err)
	})
}

func (s *Server) roleByPath(path string) *Role {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, r := range s.roles {
		if r.Path == path {
			return r
		}
	}
	return nil
}

// resource -> audience : exactement une ressource connue ; politique « refuse » pour l'ambigu (policy.json)
func (s *Server) bindResource(vals []string) (*Role, string) {
	if len(vals) != 1 {
		return nil, "exactement un paramètre resource est exigé (demande ambiguë refusée)"
	}
	u, err := url.Parse(vals[0])
	if err != nil || s.base+u.Path != vals[0] {
		return nil, "resource inconnue"
	}
	role := s.roleByPath(u.Path)
	if role == nil {
		return nil, "resource inconnue"
	}
	return role, ""
}

// fixedSession : fosite arrondit `exp` à la seconde la plus proche et laisse `iat` tronqué, d'où exp - iat = durée + 1 s une fois sur deux ;
// le banc contrôle l'égalité exacte avec policy.json. La session fixe iat et exp ensemble, à l'émission du jeton d'accès (écart « maison » du rapport).
type fixedSession struct {
	*oauth2.JWTSession
	ttl time.Duration
}

func (f *fixedSession) SetExpiresAt(k fosite.TokenType, t time.Time) {
	if k == fosite.AccessToken {
		iat := time.Now().UTC().Truncate(time.Second)
		f.JWTClaims.IssuedAt = iat
		f.JWTClaims.JTI = randHex() + randHex() // jti neuf à chaque émission (la session clonée au renouvellement garderait l'ancien)
		t = iat.Add(f.ttl)
	}
	f.JWTSession.SetExpiresAt(k, t)
}

func (f *fixedSession) Clone() fosite.Session {
	return &fixedSession{JWTSession: f.JWTSession.Clone().(*oauth2.JWTSession), ttl: f.ttl}
}

func (s *Server) newSession(role string) fosite.Session {
	// Subject de la session (pas seulement celui des claims) : /token lit GetSubject() pour refuser un rôle supprimé ou un coffre en panne
	return &fixedSession{ttl: s.accessTTL, JWTSession: &oauth2.JWTSession{
		Subject: role,
		JWTClaims: &jwt.JWTClaims{Issuer: s.base, Subject: role,
			Extra: map[string]interface{}{"token_use": "access", "role": role}},
		JWTHeader: &jwt.Headers{Extra: map[string]interface{}{"typ": "at+jwt"}},
		ExpiresAt: map[fosite.TokenType]time.Time{},
	}}
}

func oauthErr(w http.ResponseWriter, code int, e, d string) {
	writeJSON(w, code, map[string]string{"error": e, "error_description": d})
}

func (s *Server) authorize(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	r.ParseForm()
	role, why := s.bindResource(r.Form["resource"])
	if role == nil {
		oauthErr(w, 400, "invalid_target", why)
		return
	}
	r.Form["audience"] = []string{r.Form.Get("resource")}
	if s.secret != "" { // étape opérateur : le jeton ne s'émet qu'après saisie du secret d'autorisation
		if r.Method != http.MethodPost || subtle.ConstantTimeCompare([]byte(r.PostForm.Get("gw_secret")), []byte(s.secret)) != 1 {
			// action sans requête : sinon r.Form cumulerait la requête et le corps, et resource serait vu deux fois
			fmt.Fprint(w, "<!doctype html><meta charset=utf-8><title>Autoriser</title><form method=post action=/authorize>")
			for k, v := range r.Form {
				if k != "gw_secret" && len(v) > 0 {
					fmt.Fprintf(w, "<input type=hidden name=%q value=%q>", html.EscapeString(k), html.EscapeString(v[0]))
				}
			}
			fmt.Fprint(w, "<p>Secret d'autorisation du POC : <input type=password name=gw_secret autofocus> <button>Autoriser</button></form>")
			return
		}
	}
	ar, err := s.provider.NewAuthorizeRequest(ctx, r)
	if err != nil {
		s.provider.WriteAuthorizeError(ctx, w, ar, err)
		return
	}
	for _, sc := range ar.GetRequestedScopes() {
		ar.GrantScope(sc)
	}
	for _, a := range ar.GetRequestedAudience() {
		ar.GrantAudience(a)
	}
	resp, err := s.provider.NewAuthorizeResponse(ctx, ar, s.newSession(role.ID))
	if err != nil {
		s.provider.WriteAuthorizeError(ctx, w, ar, err)
		return
	}
	s.provider.WriteAuthorizeResponse(ctx, w, ar, resp)
}

func (s *Server) token(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	r.ParseForm()
	gt := r.PostForm.Get("grant_type")
	res := r.PostForm["resource"]
	if gt == "refresh_token" || len(res) > 0 {
		role, why := s.bindResource(res) // renouvellement sans resource ou avec A+B : refusé (T04)
		if role == nil {
			oauthErr(w, 400, "invalid_target", why)
			return
		}
		r.PostForm["audience"] = []string{res[0]}
		r.Form["audience"] = []string{res[0]}
	}
	ar, err := s.provider.NewAccessRequest(ctx, r, s.newSession(""))
	if err != nil {
		s.provider.WriteAccessError(ctx, w, ar, err)
		return
	}
	for _, sc := range ar.GetRequestedScopes() {
		ar.GrantScope(sc)
	}
	// refus par défaut : sujet absent, rôle supprimé ou coffre indisponible (T12, T13 côté serveur d'autorisation)
	sub := ar.GetSession().GetSubject()
	if ok, e := s.roleActive(sub); sub == "" || e != nil || !ok {
		s.provider.WriteAccessError(ctx, w, ar, fosite.ErrInvalidGrant.WithHint("rôle supprimé ou coffre indisponible"))
		return
	}
	// la ressource demandée doit être celle liée à l'autorisation d'origine (fosite reprend l'audience d'origine sans comparer
	// le paramètre resource : T03). Contrôle AVANT NewAccessResponse : un refus ne consomme ni le code ni le jeton de
	// renouvellement (la rotation, et la détection de réemploi qui révoquerait toute la lignée, ont lieu dans NewAccessResponse).
	if len(res) > 0 && !slices.Equal(ar.GetRequestedAudience(), fosite.Arguments{res[0]}) {
		oauthErr(w, 400, "invalid_target", "ressource différente de celle de l'autorisation d'origine")
		return
	}
	resp, err := s.provider.NewAccessResponse(ctx, ar)
	if err != nil {
		s.provider.WriteAccessError(ctx, w, ar, err)
		return
	}
	// registre d'émission : sans lui le jeton ne pourrait être ni accepté ni révoqué ; un enregistrement échoué n'émet rien
	jti := jtiOf(resp.GetAccessToken())
	if jti == "" || s.storeDown.Load() {
		s.provider.WriteAccessError(ctx, w, ar, fosite.ErrTemporarilyUnavailable.WithHint("registre des jetons indisponible"))
		return
	}
	if _, err := s.db.Exec(`insert or ignore into issued(jti, req) values(?, ?)`, jti, ar.GetID()); err != nil {
		s.provider.WriteAccessError(ctx, w, ar, fosite.ErrTemporarilyUnavailable.WithHint("registre des jetons indisponible"))
		return
	}
	// journal d'émission pour l'étape opérateur (constat du renouvellement, BR-020) : jamais de jeton, seulement le jti
	log.Printf("jeton émis : grant=%s rôle=%s jti=%.8s…", gt, ar.GetSession().GetSubject(), jti)
	s.provider.WriteAccessResponse(ctx, w, ar, resp)
}

func jtiOf(tok string) string {
	if p := strings.Split(tok, "."); len(p) == 3 {
		if b, e := base64.RawURLEncoding.DecodeString(p[1]); e == nil {
			var c claims
			json.Unmarshal(b, &c)
			j, _ := c["jti"].(string)
			return j
		}
	}
	return ""
}

// --- vérification des jetons d'accès (RFC 9068) par le point d'accès MCP
type claims map[string]any

func (s *Server) verify(tok, wantAud string) (claims, string) {
	p := strings.Split(tok, ".")
	if len(p) != 3 {
		return nil, "format"
	}
	hb, e1 := base64.RawURLEncoding.DecodeString(p[0])
	pb, e2 := base64.RawURLEncoding.DecodeString(p[1])
	sig, e3 := base64.RawURLEncoding.DecodeString(p[2])
	if e1 != nil || e2 != nil || e3 != nil {
		return nil, "encodage"
	}
	var hdr map[string]any
	json.Unmarshal(hb, &hdr)
	if hdr["alg"] != "RS256" {
		return nil, "alg"
	}
	if hdr["typ"] != "at+jwt" { // RFC 9068 §4 : le serveur de ressource exige le type at+jwt
		return nil, "typ"
	}
	sum := sha256.Sum256([]byte(p[0] + "." + p[1]))
	if rsaVerify(&s.key.PublicKey, sum[:], sig) != nil {
		return nil, "signature"
	}
	var c claims
	if json.Unmarshal(pb, &c) != nil {
		return nil, "claims"
	}
	if c["iss"] != s.base {
		return nil, "iss"
	}
	now := time.Now().Unix()
	if exp, ok := c["exp"].(float64); !ok || int64(exp) <= now {
		return nil, "exp"
	}
	if iat, ok := c["iat"].(float64); !ok || int64(iat) > now+60 {
		return nil, "iat"
	}
	switch a := c["aud"].(type) { // audience unique, égale à la ressource de ce point d'accès
	case string:
		if a != wantAud {
			return nil, "aud"
		}
	case []any:
		if len(a) != 1 || a[0] != wantAud {
			return nil, "aud"
		}
	default:
		return nil, "aud"
	}
	if c["token_use"] != "access" {
		return nil, "type"
	}
	return c, ""
}
