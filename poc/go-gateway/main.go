// Passerelle POC (F-002, HULL-5) : jetable. Serveur d'autorisation (fosite), relais MCP (go-sdk côté amont),
// OAuth sortant (x/oauth2), SQLite sans cgo (modernc). Voir docs/trials/poc-go.md.
package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ory/fosite"
	"github.com/ory/fosite/storage"
	_ "modernc.org/sqlite"
)

const (
	modernVersion = "2026-07-28"
	pathPrefix    = "/p/acme/app/prod/"
)

var legacyVersions = []string{"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}

func supportedVersions() []string { return append(append([]string{}, legacyVersions...), modernVersion) }

type Server struct {
	base       string
	adminToken string
	secret     string // AUTHORIZE_SECRET : si non vide, /authorize exige ce secret (étape opérateur)
	key        *rsa.PrivateKey
	provider   fosite.OAuth2Provider
	store      *storage.MemoryStore
	db         *sql.DB
	storeDown  atomic.Bool
	accessTTL  time.Duration

	mu    sync.RWMutex
	roles map[string]*Role
	conns map[string]*Conn
}

var errStore = errors.New("coffre indisponible")

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "-healthcheck" {
		r, err := http.Get("http://127.0.0.1:8080/healthz")
		if err != nil || r.StatusCode != 200 {
			os.Exit(1)
		}
		return
	}
	s := &Server{
		base:       strings.TrimRight(env("PUBLIC_BASE", "http://localhost:8080"), "/"),
		adminToken: os.Getenv("ADMIN_TOKEN"),
		secret:     os.Getenv("AUTHORIZE_SECRET"),
		roles:      map[string]*Role{},
		conns:      map[string]*Conn{},
	}
	if s.adminToken == "" {
		log.Fatal("ADMIN_TOKEN requis")
	}
	ttl := 86400
	fmt.Sscanf(env("ACCESS_TTL_SECONDS", "86400"), "%d", &ttl)
	s.accessTTL = time.Duration(ttl) * time.Second
	var err error
	// clé de signature générée au lancement (BR-010) ; lue par l'adaptateur du banc via l'administration
	if s.key, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
		log.Fatal(err)
	}
	if s.db, err = sql.Open("sqlite", "file:"+env("DB_PATH", "/tmp/gw.db")+"?_pragma=busy_timeout(5000)"); err != nil {
		log.Fatal(err)
	}
	for _, q := range []string{
		`create table if not exists roles(id text primary key, path text, deleted int default 0)`,
		`create table if not exists revoked(jti text primary key)`,
		`create table if not exists issued(jti text primary key, req text)`} {
		if _, err = s.db.Exec(q); err != nil {
			log.Fatal(err)
		}
	}
	s.initOAuth()

	pub := http.NewServeMux()
	pub.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	s.routesOAuth(pub)
	pub.HandleFunc(pathPrefix+"{role}/mcp", s.handleMCP)
	adm := http.NewServeMux()
	s.routesAdmin(adm)
	go func() { log.Fatal(http.ListenAndServe(":8081", adm)) }()
	log.Printf("passerelle POC : base %s, durée de vie du jeton d'accès %s", s.base, s.accessTTL)
	log.Fatal(http.ListenAndServe(":8080", pub))
}

func (s *Server) resourceURL(path string) string { return s.base + path }

// --- coffre (SQLite) : rôles et révocations ; la panne simulée par l'administration refuse par défaut (T13)
func (s *Server) roleActive(id string) (bool, error) {
	if s.storeDown.Load() {
		return false, errStore
	}
	var d int
	err := s.db.QueryRow(`select deleted from roles where id=?`, id).Scan(&d)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil && d == 0, err
}

func (s *Server) isRevoked(jti string) (bool, error) {
	if s.storeDown.Load() {
		return false, errStore
	}
	var x string
	err := s.db.QueryRow(`select jti from revoked where jti=?`, jti).Scan(&x)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

// isIssued : seuls les jetons émis par cette passerelle (jti enregistré à l'émission) sont acceptés.
func (s *Server) isIssued(jti string) (bool, error) {
	if s.storeDown.Load() {
		return false, errStore
	}
	var x string
	err := s.db.QueryRow(`select jti from issued where jti=?`, jti).Scan(&x)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func ctxBG() context.Context { return context.Background() }
