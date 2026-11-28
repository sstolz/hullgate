package main

import (
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/oauth2"
)

func rsaVerify(pub *rsa.PublicKey, sum, sig []byte) error {
	return rsa.VerifyPKCS1v15(pub, crypto.SHA256, sum, sig)
}

// Expose : outils d'une connexion visibles dans un rôle (liste fermée : tout ce qui n'est pas autorisé est absent).
type Expose struct {
	Conn   string   `json:"conn"`
	Prefix string   `json:"prefix"`
	Allow  []string `json:"allow"` // noms d'origine ; "*" = tous
	Hide   []string `json:"hide"`
}

type ToolEntry struct {
	Name string // nom exposé
	Orig string
	Conn string
	Def  *mcp.Tool
}

type Role struct {
	ID    string
	Path  string
	Tools []ToolEntry
}

func (r *Role) find(name string) *ToolEntry {
	for i := range r.Tools {
		if r.Tools[i].Name == name {
			return &r.Tools[i]
		}
	}
	return nil
}

// Conn : connexion amont. Jeton de connexion obtenu par OAuth sortant (x/oauth2) ou statique (instance Gitea jetable).
type Conn struct {
	ID, MCPURL string
	Static     string
	OAuth      *oauth2.Config
	Verifier   string

	mu    sync.Mutex // un seul renouvellement à la fois par connexion (T11)
	tok   *oauth2.Token
	sess  *mcp.ClientSession
	Tools []*mcp.Tool
}

const skew = 300 * time.Second // renouvelle quand il reste moins de 5 minutes

// bearer retourne le jeton de la connexion, renouvelé si nécessaire, sous verrou : jamais le jeton entrant (BR-022).
func (c *Conn) bearer(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.OAuth == nil {
		return c.Static, nil
	}
	if c.tok == nil {
		return "", fmt.Errorf("connexion %s non autorisée", c.ID)
	}
	if time.Until(c.tok.Expiry) < skew {
		// jeton de renouvellement seul : x/oauth2 échange et récupère le nouveau jeton de renouvellement (rotation)
		nt, err := c.OAuth.TokenSource(ctx, &oauth2.Token{RefreshToken: c.tok.RefreshToken}).Token()
		if err != nil {
			return "", fmt.Errorf("renouvellement de %s : %w", c.ID, err)
		}
		if nt.RefreshToken == "" {
			nt.RefreshToken = c.tok.RefreshToken
		}
		c.tok = nt
	}
	return c.tok.AccessToken, nil
}

type bearerRT struct{ c *Conn }

func (b bearerRT) RoundTrip(r *http.Request) (*http.Response, error) {
	t, err := b.c.bearer(r.Context())
	if err != nil {
		return nil, err
	}
	r = r.Clone(r.Context())
	r.Header.Set("Authorization", "Bearer "+t)
	return http.DefaultTransport.RoundTrip(r)
}

func (c *Conn) session(ctx context.Context) (*mcp.ClientSession, error) {
	c.mu.Lock()
	s := c.sess
	c.mu.Unlock()
	if s != nil {
		return s, nil
	}
	cl := mcp.NewClient(&mcp.Implementation{Name: "poc-go-gateway", Version: "0"}, nil)
	ns, err := cl.Connect(ctx, &mcp.StreamableClientTransport{Endpoint: c.MCPURL, HTTPClient: &http.Client{Transport: bearerRT{c}}}, nil)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	c.sess = ns
	c.mu.Unlock()
	return ns, nil
}

func (c *Conn) drop() { c.mu.Lock(); c.sess = nil; c.mu.Unlock() }

func (c *Conn) call(ctx context.Context, name string, args any) (*mcp.CallToolResult, error) {
	s, err := c.session(ctx)
	if err != nil {
		return nil, err
	}
	res, err := s.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		c.drop()
	}
	return res, err
}

func (c *Conn) fetchTools(ctx context.Context) error {
	s, err := c.session(ctx)
	if err != nil {
		return err
	}
	res, err := s.ListTools(ctx, nil)
	if err != nil {
		c.drop()
		return err
	}
	c.Tools = res.Tools
	return nil
}

// authorize : code + PKCE vers le serveur d'autorisation du banc, sans navigateur (le « consentement » est la redirection du banc).
func (c *Conn) authorize(ctx context.Context) error {
	v := oauth2.GenerateVerifier()
	state := randHex()
	u := c.OAuth.AuthCodeURL(state, oauth2.S256ChallengeOption(v))
	hc := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := hc.Get(u)
	if err != nil {
		return err
	}
	resp.Body.Close()
	loc, err := url.Parse(resp.Header.Get("Location"))
	if err != nil || loc.Query().Get("code") == "" {
		return fmt.Errorf("autorisation : pas de code (HTTP %d)", resp.StatusCode)
	}
	if subtle.ConstantTimeCompare([]byte(loc.Query().Get("state")), []byte(state)) != 1 {
		return fmt.Errorf("autorisation : state différent de celui envoyé")
	}
	t, err := c.OAuth.Exchange(ctx, loc.Query().Get("code"), oauth2.VerifierOption(v))
	if err != nil {
		return err
	}
	c.mu.Lock()
	c.tok = t
	c.mu.Unlock()
	return nil
}

// --- exposition des outils d'un rôle
func (s *Server) expose(role *Role, e Expose) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.conns[e.Conn]
	if c == nil {
		return fmt.Errorf("connexion %s inconnue", e.Conn)
	}
	in := func(l []string, n string) bool {
		for _, x := range l {
			if x == n || x == "*" {
				return true
			}
		}
		return false
	}
	for _, t := range c.Tools {
		if !in(e.Allow, t.Name) || in(e.Hide, t.Name) {
			continue
		}
		d := *t
		d.Name = e.Prefix + "_" + t.Name
		role.Tools = append(role.Tools, ToolEntry{Name: d.Name, Orig: t.Name, Conn: c.ID, Def: &d})
	}
	return nil
}

func rawParams(m json.RawMessage, v any) error {
	if len(m) == 0 {
		return nil
	}
	return json.Unmarshal(m, v)
}
