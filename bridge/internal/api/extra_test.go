package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestExtraRoutes(t *testing.T) {
	hello := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "too big", http.StatusRequestEntityTooLarge)
			return
		}
		w.Write([]byte("hello " + r.URL.Path + " " + string(b)))
	})
	s := &Server{Token: token, Extra: []Route{
		{Pattern: "/api/legacy-open", Handler: hello, NoToken: true},
		{Pattern: "POST /api/legacy-token", Handler: hello},
	}}
	if err := s.CheckExtra(); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewUnstartedServer(nil)
	srv.Config.Handler = s.Handler(srv.Listener.Addr().String())
	srv.Start()
	defer srv.Close()
	call := func(method, path, auth, host, body string) (int, string) {
		req, _ := http.NewRequest(method, srv.URL+path, strings.NewReader(body))
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		if host != "" {
			req.Host = host
		}
		resp, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}
	if code, body := call("POST", "/api/legacy-open", "", "", "x"); code != 200 || body != "hello /api/legacy-open x" {
		t.Fatalf("open route without token: %d %q", code, body)
	}
	if code, _ := call("POST", "/api/legacy-open", "", "attacker.example:80", ""); code != 403 {
		t.Fatalf("open route keeps the Host check: %d", code)
	}
	if code, _ := call("POST", "/api/legacy-open", "", "", strings.Repeat("a", maxBody+10)); code != 413 {
		t.Fatalf("open route keeps the body limit: %d", code)
	}
	if code, _ := call("POST", "/api/legacy-token", "", "", ""); code != 401 {
		t.Fatalf("token route without token: %d", code)
	}
	if code, body := call("POST", "/api/legacy-token", token, "", "y"); code != 200 || body != "hello /api/legacy-token y" {
		t.Fatalf("token route with token: %d %q", code, body)
	}
	if code, _ := call("GET", "/api/v1/status", "", "", ""); code != 401 {
		t.Fatalf("v1 still needs the token: %d", code)
	}

	for _, bad := range []Route{
		{Pattern: "/", Handler: hello},
		{Pattern: "/api/v1/status", Handler: hello, NoToken: true},
		{Pattern: "GET /api/v1/extra", Handler: hello},
		{Pattern: "/health", Handler: hello, NoToken: true},
		{Pattern: "noslash", Handler: hello},
		{Pattern: "/api/x", Handler: nil},
	} {
		if err := (&Server{Extra: []Route{bad}}).CheckExtra(); err == nil {
			t.Errorf("pattern %q accepted", bad.Pattern)
		}
	}
	dup := []Route{{Pattern: "/api/x", Handler: hello}, {Pattern: "/api/x", Handler: hello, NoToken: true}}
	if err := CheckRoutes(dup); err == nil {
		t.Error("duplicate pattern accepted")
	}
}
