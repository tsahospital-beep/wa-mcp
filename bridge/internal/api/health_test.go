package api

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestHealthNeedsNoTokenAndShowsNoData(t *testing.T) {
	e := newEnv(t, false)
	resp, err := http.Get(e.srv.URL + HealthPath)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	resp.Body.Close()
	if resp.StatusCode != 200 || out["ok"] != true || out["connected"] != true || out["logged_in"] != true || len(out) != 3 {
		t.Fatalf("health: %d %v", resp.StatusCode, out)
	}
	if code, _ := e.do(t, "POST", HealthPath, nil); code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /health: %d", code)
	}
	if code, _ := e.do(t, "GET", HealthPath, nil, func(r *http.Request) { r.Host = "evil.example.test" }); code != http.StatusForbidden {
		t.Fatalf("foreign Host: %d", code)
	}
	// Everything else still needs the token.
	if code, _ := e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Header.Del("Authorization") }); code != http.StatusUnauthorized {
		t.Fatalf("status without token: %d", code)
	}
}

func TestServeHealthForPreview(t *testing.T) {
	probe, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := probe.Addr().String()
	probe.Close()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- ServeHealth(ctx, addr) }()
	target, _ := HealthURL(addr)
	var resp *http.Response
	for i := 0; i < 50; i++ {
		if resp, err = http.Get(target); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	resp.Body.Close()
	if resp.StatusCode != 200 || out["ok"] != true || out["connected"] != false {
		t.Fatalf("preview health: %d %v", resp.StatusCode, out)
	}
	if resp, err = http.Get(strings.TrimSuffix(target, HealthPath) + "/api/v1/status"); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("other paths: %d", resp.StatusCode)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if u, _ := HealthURL("0.0.0.0:8080"); u != "http://127.0.0.1:8080/health" {
		t.Fatalf("HealthURL: %s", u)
	}
}
