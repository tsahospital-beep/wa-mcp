package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestWildcardRoutesCannotTakeBridgePaths(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(299) })
	for _, pat := range []string{"/{p...}", "/api/{v}/status", "GET /{x}/v1/status", "127.0.0.1:8080/{p...}", "127.0.0.1/{p...}",
		"POST /{a}/{b}/send", "PATCH /{prefix}/v1/chats", "REPORT /{prefix}/v1/new", "/api", "/%61pi/v1/chats", "/api/v1/extra", "/health"} {
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
	mux := http.NewServeMux()
	if s.serveOpen(mux, s.mountExtra(mux), httptest.NewRecorder(),
		httptest.NewRequest("GET", "http://127.0.0.1:8080/api/v1/status", nil)) {
		t.Fatal("serveOpen served a bridge path")
	}
}

func TestOpenRouteBrowserAndRemoteAuth(t *testing.T) {
	s := &Server{Token: "test", Extra: []Route{{Pattern: "/open", NoToken: true,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) })}}}
	h := s.Handler("127.0.0.1:9017")
	for _, tc := range []struct {
		peer, origin, token string
		want                int
	}{
		{"127.0.0.1:1", "https://other.example", "", 403},
		{"127.0.0.1:1", "http://127.0.0.1:9017", "", 204},
		{"192.0.2.1:1", "", "test", 204},
		{"192.0.2.1:1", "", "", 401},
	} {
		r := httptest.NewRequest("POST", "http://127.0.0.1:9017/open", nil)
		r.RemoteAddr = tc.peer
		if tc.origin != "" {
			r.Header.Set("Origin", tc.origin)
		}
		if tc.token != "" {
			r.Header.Set("Authorization", "Bearer "+tc.token)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Errorf("%+v: %d", tc, w.Code)
		}
	}
}

func TestExtraRoutesRespectSpecificity(t *testing.T) {
	routes := []Route{
		{Pattern: "/app/{id}", NoToken: true, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(r.PathValue("id"))) })},
		{Pattern: "/app/private", Handler: http.NotFoundHandler()},
	}
	h, err := ExtraOnly("127.0.0.1:9001", "test", routes)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"private", "public"} {
		req := httptest.NewRequest("GET", "http://127.0.0.1:9001/app/"+path, nil)
		req.RemoteAddr = "127.0.0.1:1"
		res := httptest.NewRecorder()
		h.ServeHTTP(res, req)
		if path == "private" && res.Code != 401 || path == "public" && res.Body.String() != path {
			t.Fatalf("%s: %d %q", path, res.Code, res.Body.String())
		}
	}
}
