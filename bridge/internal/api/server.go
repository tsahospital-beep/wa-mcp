// Package api serves the bridge HTTP API v1 on the loopback interface.
// Every request needs the bearer token and a Host header that matches the
// listen address.
package api

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

//go:embed openapi.yaml
var openAPISpec []byte

// maxBody limits JSON request bodies.
const maxBody = 256 << 10

// Bridge is the WhatsApp side the API needs (implemented by wa.Client).
type Bridge interface {
	Connected() bool
	LoggedIn() bool
	Account() string
	MarkRead(ctx context.Context, ids []string, at time.Time, chat, sender types.JID) error
	RequestHistory(ctx context.Context, anchor *store.Message, count int) error
	// LoginURL issues a one-time link to the open login window.
	LoginURL() (string, error)
}

// MediaFetcher saves attachments (implemented by media.Fetcher).
type MediaFetcher interface {
	Fetch(ctx context.Context, conv, id string) (*media.Result, error)
}

// Server holds the API dependencies.
type Server struct {
	Agent      AgentFeed
	Emit       func(string, any)
	RejectCall func(context.Context, types.JID, string) error
	Cfg        *config.Config
	DB         *store.DB
	WA         Bridge
	Send       func(ctx context.Context, req sender.Request) (sender.Result, error)
	Media      MediaFetcher
	Avatar     func(ctx context.Context, jid types.JID) (string, error)
	Token      string
	Version    string
	Now        func() time.Time
	Location   *time.Location
	Log        *log.Logger
	// Security holds session events and the protection summary for /status (may be nil).
	Security *security.Monitor
	// WebhookDropped counts webhook events lost to full queues (may be nil).
	WebhookDropped func() int64
	// Extra are routes added by an embedding program (see extra.go).
	Extra []Route

	inflight  sync.WaitGroup
	rejecting sync.Map // call IDs with a rejection in progress
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Server) logf(format string, args ...any) {
	if s.Log != nil {
		s.Log.Printf(format, args...)
	}
}

// MinTokenLen is the shortest accepted API token.
const MinTokenLen = 32

// LoadToken returns api.token from the config (or WA_MCP_API_TOKEN), or reads
// (and on first start creates) the token file in the store folder. Tokens
// shorter than MinTokenLen are refused.
func LoadToken(cfg *config.Config) (string, error) {
	if strings.TrimSpace(cfg.API.Token) == "" {
		b, err := tokenKeystore(cfg) // token.go
		if err != nil {
			return "", err
		}
		if b != nil {
			return loadKeystoreToken(cfg, b)
		}
	}
	if t, err := ReadToken(cfg); t != "" || err != nil {
		return t, err
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	tok := hex.EncodeToString(buf)
	if err := fsutil.WriteFile(cfg.TokenFile(), []byte(tok+"\n")); err != nil {
		return "", err
	}
	return tok, nil
}

// ReadToken is LoadToken without creating the file: "" when there is no token yet.
func ReadToken(cfg *config.Config) (string, error) {
	if t := strings.TrimSpace(cfg.API.Token); t != "" {
		if len(t) < MinTokenLen {
			return "", fmt.Errorf("api.token must be at least %d characters (use a random hex string)", MinTokenLen)
		}
		return t, nil
	}
	ks, err := tokenKeystore(cfg)
	if err != nil {
		return "", err
	}
	if ks != nil {
		if t, err := getKeystoreToken(ks); t != "" || err != nil {
			return t, err
		}
	}
	b, err := os.ReadFile(cfg.TokenFile())
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if t := strings.TrimSpace(string(b)); len(t) >= MinTokenLen {
		return t, nil
	}
	return "", nil
}

// allowedHosts lists Host header values accepted for a bound address.
func allowedHosts(bound string) map[string]bool {
	host, port, err := net.SplitHostPort(bound)
	if err != nil {
		return map[string]bool{bound: true}
	}
	out := map[string]bool{strings.ToLower(net.JoinHostPort(host, port)): true}
	if common.IsLoopbackHost(host) || common.IsUnspecifiedHost(host) {
		for _, h := range []string{"localhost", "127.0.0.1", "::1"} {
			out[net.JoinHostPort(h, port)] = true
		}
	}
	return out
}

// Handler builds the routed handler for a server bound at addr.
func (s *Server) Handler(addr string) http.Handler {
	hosts := allowedHosts(addr)
	mux := http.NewServeMux()
	route := func(path string, handlers map[string]http.HandlerFunc) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			h, ok := handlers[r.Method]
			if !ok {
				methods := make([]string, 0, len(handlers))
				for m := range handlers {
					methods = append(methods, m)
				}
				w.Header().Set("Allow", strings.Join(methods, ", "))
				fail(w, http.StatusMethodNotAllowed, "method not allowed")
				return
			}
			h(w, r)
		})
	}
	route("/api/v1/agent/next", map[string]http.HandlerFunc{"POST": s.nextAgent})
	route("/api/v1/agent/reply", map[string]http.HandlerFunc{"POST": s.agentReply})
	route("/api/v1/agent/reject", map[string]http.HandlerFunc{"POST": s.rejectAgentCall})
	route("/api/v1/status", map[string]http.HandlerFunc{"GET": s.status})
	route("/api/v1/chats", map[string]http.HandlerFunc{"GET": s.chats})
	route("/api/v1/messages", map[string]http.HandlerFunc{"GET": s.messages})
	route("/api/v1/send/preview", map[string]http.HandlerFunc{"POST": s.previewText})
	route("/api/v1/send", map[string]http.HandlerFunc{"POST": s.send})
	route("/api/v1/messages/react", map[string]http.HandlerFunc{"POST": s.action("react")})
	route("/api/v1/messages/edit", map[string]http.HandlerFunc{"POST": s.action("edit")})
	route("/api/v1/messages/delete", map[string]http.HandlerFunc{"POST": s.action("delete")})
	route("/api/v1/messages/forward", map[string]http.HandlerFunc{"POST": s.action("forward")})
	route("/api/v1/read", map[string]http.HandlerFunc{"POST": s.read})
	route("/api/v1/history", map[string]http.HandlerFunc{"POST": s.history})
	route("/api/v1/media/download", map[string]http.HandlerFunc{"POST": s.download})
	route("/api/v1/avatar", map[string]http.HandlerFunc{"GET": s.avatar})
	route("/api/v1/calls", map[string]http.HandlerFunc{"GET": s.calls})
	route("/api/v1/schedule", map[string]http.HandlerFunc{"GET": s.listSchedule, "POST": s.addSchedule})
	route("/api/v1/schedule/{id}", map[string]http.HandlerFunc{"DELETE": s.cancelSchedule})
	route("/api/v1/import", map[string]http.HandlerFunc{"POST": s.importExport})
	route("/api/v1/login/url", map[string]http.HandlerFunc{"POST": s.loginURL})
	route("/api/v1/openapi.yaml", map[string]http.HandlerFunc{"GET": s.openapi})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { fail(w, http.StatusNotFound, "not found") })
	open := s.mountExtra(mux)

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if !hosts[strings.ToLower(r.Host)] {
			fail(w, http.StatusForbidden, "host not allowed")
			return
		}
		if r.URL.Path == HealthPath {
			s.health(w, r)
			return
		}
		if s.serveOpen(mux, open, w, r) {
			return
		}
		if !s.authorized(r) {
			w.Header().Set("WWW-Authenticate", `Bearer realm="wa-mcp"`)
			fail(w, http.StatusUnauthorized, "missing or wrong bearer token")
			return
		}
		if r.Body != nil {
			r.Body = http.MaxBytesReader(w, r.Body, s.bodyLimit(r))
		}
		s.inflight.Add(1)
		defer s.inflight.Done()
		mux.ServeHTTP(w, r)
	})
}

func (s *Server) authorized(r *http.Request) bool {
	h := r.Header.Get("Authorization")
	const prefix = "Bearer "
	if s.Token == "" || len(h) <= len(prefix) || !strings.EqualFold(h[:len(prefix)], prefix) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(strings.TrimSpace(h[len(prefix):])), []byte(s.Token)) == 1
}

// HandlerTimeout bounds how long Serve waits for running requests after ctx
// ends (a send with its human-like pauses fits in it).
const HandlerTimeout = 4 * time.Minute

// Serve listens on cfg.API.Listen until ctx ends. Request contexts derive from
// ctx; after ctx ends Serve waits for running requests before it returns, so
// the caller can then close the WhatsApp client and the database.
func (s *Server) Serve(ctx context.Context, ready func(addr string)) error {
	ln, err := net.Listen("tcp", s.Cfg.API.Listen)
	if err != nil {
		return fmt.Errorf("api listen %s: %w", s.Cfg.API.Listen, err)
	}
	addr := ln.Addr().String()
	srv := &http.Server{
		Handler:           s.Handler(addr),
		BaseContext:       func(net.Listener) context.Context { return ctx },
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      5 * time.Minute,
		IdleTimeout:       60 * time.Second,
	}
	if ready != nil {
		ready(addr)
	}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
		if !common.WaitTimeout(s.inflight.Wait, HandlerTimeout) {
			s.logf("api: requests still running after %s", HandlerTimeout)
		}
		return nil
	}
}

// ---- helpers ----

func reply(w http.ResponseWriter, status int, v map[string]any) {
	if v == nil {
		v = map[string]any{}
	}
	v["ok"] = status < 400
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, status int, msg string) {
	reply(w, status, map[string]any{"error": msg})
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, http.StatusRequestEntityTooLarge, "request body too large")
		} else {
			fail(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		}
		return false
	}
	return true
}

// parseTime accepts RFC3339, a date (YYYY-MM-DD) or unix seconds/milliseconds.
func parseTime(v string, loc *time.Location) (time.Time, error) {
	v = strings.TrimSpace(v)
	if v == "" {
		return time.Time{}, nil
	}
	if n, err := strconv.ParseInt(v, 10, 64); err == nil {
		return common.UnixFromNumber(n), nil
	}
	if t, err := time.Parse(time.RFC3339, v); err == nil {
		return t, nil
	}
	if loc == nil {
		loc = time.Local
	}
	if t, err := time.ParseInLocation("2006-01-02", v, loc); err == nil {
		return t, nil
	}
	return time.Time{}, fmt.Errorf("bad time %q: use RFC3339 (2026-01-02T15:04:05Z) or unix seconds", v)
}

func intParam(r *http.Request, name string, def, maxVal int) (int, error) {
	v := r.URL.Query().Get(name)
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer", name)
	}
	if maxVal > 0 && n > maxVal {
		n = maxVal
	}
	return n, nil
}

func unix(t time.Time) int64 {
	if t.IsZero() {
		return 0
	}
	return t.Unix()
}
