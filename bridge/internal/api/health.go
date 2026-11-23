package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
)

// HealthPath answers container health checks. It needs no token and tells
// only whether the bridge runs, is connected and has a linked device.
const HealthPath = "/health"

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeHealth(w, r, s.WA.Connected(), s.WA.LoggedIn(), s.disconnectReason() != "")
}

func (s *Server) webhookDropped() int64 {
	if s.WebhookDropped == nil {
		return 0
	}
	return s.WebhookDropped()
}

// disconnectReason is the reason of a permanent disconnect when the bridge
// can tell one (wa.Client can).
func (s *Server) disconnectReason() string {
	if d, ok := s.WA.(interface{ DisconnectReason() string }); ok {
		return d.DisconnectReason()
	}
	return ""
}

// writeHealth answers 200, or 503 when a bridge was disconnected for
// good (WhatsApp will not reconnect it by itself), so a container health
// check turns red.
func writeHealth(w http.ResponseWriter, r *http.Request, connected, loggedIn, permanent bool) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		fail(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	status := http.StatusOK
	body := map[string]any{"connected": connected && !permanent, "logged_in": loggedIn}
	if permanent {
		status = http.StatusServiceUnavailable
		body["permanent_disconnect"] = true
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	body["ok"] = status == http.StatusOK
	_ = json.NewEncoder(w).Encode(body)
}

// ServeHealth answers only GET /health on listen until ctx ends. The login
// preview uses it so that a container health check works without WhatsApp.
func ServeHealth(ctx context.Context, listen string) error {
	ln, err := net.Listen("tcp", listen)
	if err != nil {
		return fmt.Errorf("health listen %s: %w", listen, err)
	}
	hosts := allowedHosts(ln.Addr().String())
	srv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Cache-Control", "no-store")
			if !hosts[strings.ToLower(r.Host)] {
				fail(w, http.StatusForbidden, "host not allowed")
				return
			}
			if r.URL.Path != HealthPath {
				fail(w, http.StatusNotFound, "not found")
				return
			}
			writeHealth(w, r, false, false, false)
		}),
		ReadHeaderTimeout: 5 * time.Second,
	}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		sctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
		return nil
	}
}

// HealthURL is the address of /health for a listen address; an unspecified
// host (0.0.0.0, ::) is reached through 127.0.0.1.
func HealthURL(listen string) (string, error) {
	host, port, err := net.SplitHostPort(listen)
	if err != nil {
		return "", err
	}
	if common.IsUnspecifiedHost(host) {
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, port) + HealthPath, nil
}
