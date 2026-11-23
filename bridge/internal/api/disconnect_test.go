package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

type outdatedBridge struct{ fakeBridge }

func (outdatedBridge) DisconnectReason() string { return "client outdated" }

func TestPermanentDisconnectTurnsHealthRed(t *testing.T) {
	s := &Server{Cfg: config.Default(), WA: &outdatedBridge{}, Token: token, Version: "test"}
	h := s.Handler("127.0.0.1:8080")
	req := httptest.NewRequest("GET", "http://127.0.0.1:8080"+HealthPath, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	json.NewDecoder(rec.Body).Decode(&out)
	if rec.Code != http.StatusServiceUnavailable || out["connected"] != false || out["permanent_disconnect"] != true {
		t.Fatalf("health %d %v", rec.Code, out)
	}
	req = httptest.NewRequest("GET", "http://127.0.0.1:8080/api/v1/status", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	json.NewDecoder(rec.Body).Decode(&out)
	if out["disconnect_reason"] != "client outdated" {
		t.Fatalf("status %v", out)
	}
	// A healthy bridge still answers 200.
	ok := &Server{Cfg: config.Default(), WA: &fakeBridge{}, Token: token}
	rec = httptest.NewRecorder()
	ok.Handler("127.0.0.1:8080").ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:8080"+HealthPath, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthy: %d", rec.Code)
	}
}

func TestPermanentDisconnectBeforeLoginIsUnhealthy(t *testing.T) {
	rec := httptest.NewRecorder()
	writeHealth(rec, httptest.NewRequest("GET", HealthPath, nil), false, false, true)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("health %d", rec.Code)
	}
}
