package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/oauth2"
)

// secretLike : toute chaîne de 24 caractères ou plus du jeu base64url / JWT est masquée avant journalisation (BR-022).
var secretLike = regexp.MustCompile(`[A-Za-z0-9_\-.~+/=]{24,}`)

func logUpstreamErr(conn, tool string, err error) {
	msg := err.Error()
	var re *oauth2.RetrieveError
	if errors.As(err, &re) { // corps de la réponse du point de jeton amont : jamais journalisé
		code := 0
		if re.Response != nil {
			code = re.Response.StatusCode
		}
		msg = fmt.Sprintf("point de jeton amont : HTTP %d %s", code, re.ErrorCode)
	}
	log.Printf("erreur amont : connexion=%s outil=%s : %s", conn, tool, secretLike.ReplaceAllString(msg, "[masqué]"))
}

type rpcReq struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

func rpcOK(id json.RawMessage, result any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "result": result}
}

func rpcErr(id json.RawMessage, code int, msg string, data any) map[string]any {
	e := map[string]any{"code": code, "message": msg}
	if data != nil {
		e["data"] = data
	}
	return map[string]any{"jsonrpc": "2.0", "id": id, "error": e}
}

func (s *Server) challenge(w http.ResponseWriter, path, errCode string) {
	h := `Bearer resource_metadata="` + s.base + "/.well-known/oauth-protected-resource" + path + `"`
	if errCode != "" {
		h += `, error="` + errCode + `"`
	}
	w.Header().Set("WWW-Authenticate", h)
	writeJSON(w, 401, map[string]string{"error": errOr(errCode, "unauthorized")})
}

func errOr(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func (s *Server) handleMCP(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	role := s.roleByPath(path)
	if role == nil {
		writeJSON(w, 404, map[string]string{"error": "not_found"})
		return
	}
	res := s.resourceURL(path)
	// 1. authentification : jeton d'accès JWT à audience unique égale à ce point d'accès
	ah := r.Header.Get("Authorization")
	if len(ah) < 8 || !strings.EqualFold(ah[:7], "bearer ") {
		s.challenge(w, path, "")
		return
	}
	cl, why := s.verify(ah[7:], res)
	if cl == nil {
		s.challenge(w, path, "invalid_token")
		return
	}
	// 2. coffre : refus par défaut en panne (policy.json « deny_protected_operations »)
	jti, _ := cl["jti"].(string)
	rr, e1 := s.isRevoked(jti)
	ok, e2 := s.roleActive(role.ID)
	iss, e3 := s.isIssued(jti)
	if e1 != nil || e2 != nil || e3 != nil {
		writeJSON(w, 503, map[string]string{"error": "store_unavailable", "error_description": "refus par défaut"})
		return
	}
	if rr || !ok || !iss || cl["role"] != role.ID {
		s.challenge(w, path, "invalid_token")
		return
	}
	_ = why
	// 3. demandes ambiguës refusées : aucune désignation de connexion par le client
	if r.Header.Get("X-Connection-Id") != "" || r.URL.Query().Has("connection_id") {
		writeJSON(w, 400, rpcErr(nil, -32602, "désignation de connexion refusée (demande ambiguë)", nil))
		return
	}
	switch r.Method {
	case http.MethodDelete:
		w.WriteHeader(204)
		return
	case http.MethodGet:
		w.Header().Set("Allow", "POST, DELETE")
		w.WriteHeader(405)
		return
	case http.MethodPost:
	default:
		w.WriteHeader(405)
		return
	}
	var req rpcReq
	if json.NewDecoder(r.Body).Decode(&req) != nil {
		writeJSON(w, 400, rpcErr(nil, -32700, "JSON invalide", nil))
		return
	}
	ver := r.Header.Get("MCP-Protocol-Version")
	if ver != "" && !slices.Contains(supportedVersions(), ver) {
		writeJSON(w, 400, rpcErr(req.ID, -32602, "version de protocole non servie", map[string]any{"supported": supportedVersions(), "requested": ver}))
		return
	}
	if strings.HasPrefix(req.Method, "notifications/") {
		w.WriteHeader(202)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	switch req.Method {
	case "initialize":
		var p struct{ ProtocolVersion string `json:"protocolVersion"` }
		rawParams(req.Params, &p)
		pv := p.ProtocolVersion
		if !slices.Contains(legacyVersions, pv) {
			pv = legacyVersions[len(legacyVersions)-1]
		}
		w.Header().Set("Mcp-Session-Id", randHex())
		writeJSON(w, 200, rpcOK(req.ID, map[string]any{"protocolVersion": pv, "capabilities": map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo": map[string]any{"name": "poc-go-gateway", "version": "0"}}))
	case "server/discover":
		writeJSON(w, 200, rpcOK(req.ID, map[string]any{"supportedVersions": supportedVersions(), "capabilities": map[string]any{"tools": map[string]any{}},
			"_meta": map[string]any{"io.modelcontextprotocol/serverInfo": map[string]any{"name": "poc-go-gateway", "version": "0"}}}))
	case "ping":
		writeJSON(w, 200, rpcOK(req.ID, map[string]any{}))
	case "tools/list":
		tools := []any{map[string]any{"name": "ping", "description": "Répond pong (outil de la passerelle)", "inputSchema": map[string]any{"type": "object", "properties": map[string]any{}}}}
		s.mu.RLock()
		for _, t := range role.Tools {
			tools = append(tools, t.Def)
		}
		s.mu.RUnlock()
		writeJSON(w, 200, rpcOK(req.ID, map[string]any{"tools": tools}))
	case "tools/call":
		s.toolsCall(ctx, w, role, req)
	default:
		writeJSON(w, 200, rpcErr(req.ID, -32601, "méthode inconnue", nil))
	}
}

func (s *Server) toolsCall(ctx context.Context, w http.ResponseWriter, role *Role, req rpcReq) {
	var p struct {
		Name      string         `json:"name"`
		Arguments map[string]any `json:"arguments"`
	}
	if rawParams(req.Params, &p) != nil {
		writeJSON(w, 200, rpcErr(req.ID, -32602, "paramètres invalides", nil))
		return
	}
	if _, bad := p.Arguments["connection_id"]; bad { // désignation de connexion par argument : refusée
		writeJSON(w, 200, rpcErr(req.ID, -32602, "argument connection_id refusé (demande ambiguë)", nil))
		return
	}
	if p.Name == "ping" {
		writeJSON(w, 200, rpcOK(req.ID, map[string]any{"content": []any{map[string]any{"type": "text", "text": "pong"}}, "isError": false}))
		return
	}
	s.mu.RLock()
	te := role.find(p.Name)
	var conn *Conn
	if te != nil {
		conn = s.conns[te.Conn]
	}
	s.mu.RUnlock()
	if te == nil || conn == nil { // absent ou masqué : refus avant tout appel amont
		writeJSON(w, 200, rpcErr(req.ID, -32602, "outil inconnu", nil))
		return
	}
	res, err := conn.call(ctx, te.Orig, p.Arguments)
	if err != nil {
		// détail (URL amont, corps du point de jeton) journalisé côté passerelle, masqué ; au client, un message générique
		logUpstreamErr(te.Conn, p.Name, err)
		writeJSON(w, 200, rpcErr(req.ID, -32603, "erreur amont", nil))
		return
	}
	writeJSON(w, 200, rpcOK(req.ID, res))
}

var _ = mcp.Implementation{}
