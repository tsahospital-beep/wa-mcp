package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestWildcardRoutesCannotTakeBridgePaths(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(299) })
	for _, pat := range []string{"/{p...}", "/api/{v}/status", "GET /{x}/v1/status", "127.0.0.1:8080/{p...}", "127.0.0.1/{p...}",
		"POST /{a}/{b}/send", "/api/v1/extra", "/health"} {
		if err := CheckRoutes([]Route{{Pattern: pat, Handler: ok, NoToken: true}}); err == nil {
			t.Errorf("pattern %q accepted", pat)
		}
	}
	if err := CheckRoutes([]Route{{Pattern: "/hooks/{id}", Handler: ok}, {Pattern: "POST /app/{rest...}", Handler: ok}}); err != nil {
		t.Fatalf("a safe wildcard was refused: %v", err)
	}
}

func TestOpenRoutesNeedALoopbackPeer(t *testing.T) {
	routes := []Route{{Pattern: "/open", NoToken: true,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(299) })}}
	s := &Server{Token: "0123456789abcdef0123456789abcdef", Extra: routes}
	h := s.Handler("0.0.0.0:8080")
	for addr, want := range map[string]int{"127.0.0.1:5000": 299, "[::1]:5000": 299, "192.0.2.10:5000": 401} {
		req := httptest.NewRequest("GET", "http://127.0.0.1:8080/open", nil)
		req.Host, req.RemoteAddr = "127.0.0.1:8080", addr
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Errorf("peer %s: %d, want %d", addr, rec.Code, want)
		}
	}
	// A reserved path never reaches an open route.
	if s.serveOpen(s.mountExtra(http.NewServeMux()), httptest.NewRecorder(),
		httptest.NewRequest("GET", "http://127.0.0.1:8080/api/v1/status", nil)) {
		t.Fatal("serveOpen served a bridge path")
	}
}
