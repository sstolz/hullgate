package main

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"net/http"

	"golang.org/x/oauth2"
)

func randHex() string { b := make([]byte, 8); rand.Read(b); return hex.EncodeToString(b) }

// Administration du POC : port séparé, publié sur 127.0.0.1 seulement ; appelée par l'adaptateur du banc.
func (s *Server) routesAdmin(mux *http.ServeMux) {
	h := func(f func(w http.ResponseWriter, r *http.Request)) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Admin-Token")), []byte(s.adminToken)) != 1 {
				writeJSON(w, 403, map[string]string{"error": "forbidden"})
				return
			}
			f(w, r)
		}
	}
	body := func(r *http.Request) map[string]any {
		m := map[string]any{}
		json.NewDecoder(r.Body).Decode(&m)
		return m
	}
	str := func(m map[string]any, k string) string { v, _ := m[k].(string); return v }

	mux.HandleFunc("POST /admin/roles", h(func(w http.ResponseWriter, r *http.Request) {
		m := body(r)
		id, path := str(m, "id"), pathPrefix+str(m, "id")+"/mcp"
		s.db.Exec(`insert or replace into roles(id,path,deleted) values(?,?,0)`, id, path)
		s.mu.Lock()
		s.roles[id] = &Role{ID: id, Path: path}
		s.mu.Unlock()
		writeJSON(w, 200, map[string]string{"id": id, "resource": s.resourceURL(path)})
	}))
	mux.HandleFunc("DELETE /admin/roles/{id}", h(func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		s.db.Exec(`update roles set deleted=1 where id=?`, id)
		s.mu.Lock()
		delete(s.roles, id)
		s.mu.Unlock()
		writeJSON(w, 200, map[string]string{"deleted": id})
	}))
	mux.HandleFunc("POST /admin/connections", h(func(w http.ResponseWriter, r *http.Request) {
		m := body(r)
		c := &Conn{ID: str(m, "id"), MCPURL: str(m, "mcp_url"), Static: str(m, "token")}
		if str(m, "token_url") != "" {
			c.OAuth = &oauth2.Config{ClientID: str(m, "client_id"), ClientSecret: str(m, "client_secret"),
				Endpoint: oauth2.Endpoint{AuthURL: str(m, "auth_url"), TokenURL: str(m, "token_url"), AuthStyle: oauth2.AuthStyleInParams},
				RedirectURL: "http://gateway.invalid/callback", Scopes: []string{"mcp:read"}}
		}
		s.mu.Lock()
		s.conns[c.ID] = c
		s.mu.Unlock()
		if c.OAuth != nil {
			if err := c.authorize(r.Context()); err != nil {
				writeJSON(w, 502, map[string]string{"error": err.Error()})
				return
			}
		}
		if err := c.fetchTools(r.Context()); err != nil {
			writeJSON(w, 502, map[string]string{"error": "outils : " + err.Error()})
			return
		}
		names := []string{}
		for _, t := range c.Tools {
			names = append(names, t.Name)
		}
		writeJSON(w, 200, map[string]any{"id": c.ID, "tools": names})
	}))
	// nouvelle autorisation OAuth sortante (reprise après perte, ou jeton à courte vie, T11)
	mux.HandleFunc("POST /admin/connections/{id}/authorize", h(func(w http.ResponseWriter, r *http.Request) {
		s.mu.RLock()
		c := s.conns[r.PathValue("id")]
		s.mu.RUnlock()
		if c == nil || c.OAuth == nil {
			writeJSON(w, 404, map[string]string{"error": "inconnue"})
			return
		}
		if err := c.authorize(r.Context()); err != nil {
			writeJSON(w, 502, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]bool{"authorized": true})
	}))
	mux.HandleFunc("POST /admin/roles/{id}/expose", h(func(w http.ResponseWriter, r *http.Request) {
		var e Expose
		json.NewDecoder(r.Body).Decode(&e)
		s.mu.RLock()
		role := s.roles[r.PathValue("id")]
		s.mu.RUnlock()
		if role == nil {
			writeJSON(w, 404, map[string]string{"error": "rôle inconnu"})
			return
		}
		if err := s.expose(role, e); err != nil {
			writeJSON(w, 400, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]int{"tools": len(role.Tools)})
	}))
	mux.HandleFunc("POST /admin/revoke", h(func(w http.ResponseWriter, r *http.Request) {
		if s.storeDown.Load() {
			writeJSON(w, 503, map[string]string{"error": "store_unavailable"})
			return
		}
		if _, err := s.db.Exec(`insert or ignore into revoked(jti) values(?)`, str(body(r), "jti")); err != nil {
			writeJSON(w, 503, map[string]string{"error": "store_unavailable"})
			return
		}
		writeJSON(w, 200, map[string]bool{"revoked": true})
	}))
	mux.HandleFunc("POST /admin/store", h(func(w http.ResponseWriter, r *http.Request) {
		d, _ := body(r)["down"].(bool)
		s.storeDown.Store(d)
		writeJSON(w, 200, map[string]bool{"down": d})
	}))
	// clé de signature de test (BR-010) : lue par l'adaptateur pour falsifier des jetons (T05, T06)
	mux.HandleFunc("GET /admin/key", h(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]string{"n": s.key.N.Text(16), "d": s.key.D.Text(16), "e": "10001"})
	}))
}
