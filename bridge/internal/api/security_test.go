package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/keystore"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestStatusShowsSecurityEventsAndProtection(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var hooked []string
	mon := &security.Monitor{Store: db, Emit: func(e string, d any) { hooked = append(hooked, e) },
		Now: func() time.Time { return time.Unix(1_800_000_000, 0) }}
	mon.SetProtection(map[string]string{"field_encryption": "keystore", "key_store": "dpapi", "efs": "on"})
	for i := 0; i < security.MaxEvents+3; i++ {
		mon.Record(security.DeviceRemoved, "a linked device was removed")
	}
	mon.Record(security.SessionReplaced, "another client connected with this session")

	cfg := config.Default()
	s := &Server{Cfg: cfg, DB: db, WA: &fakeBridge{}, Token: token, Version: "test", Security: mon}
	srv := httptest.NewUnstartedServer(nil)
	srv.Config.Handler = s.Handler(srv.Listener.Addr().String())
	srv.Start()
	defer srv.Close()
	req, _ := http.NewRequest("GET", srv.URL+"/api/v1/status", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Events     []security.Event  `json:"security_events"`
		Alert      bool              `json:"security_alert"`
		Protection map[string]string `json:"protection"`
	}
	json.NewDecoder(resp.Body).Decode(&out)
	if len(out.Events) != security.MaxEvents || out.Events[0].Kind != security.SessionReplaced || out.Events[0].Level != "warning" {
		t.Fatalf("events %+v", out.Events)
	}
	if !out.Alert || out.Protection["key_store"] != "dpapi" {
		t.Fatalf("alert %v protection %v", out.Alert, out.Protection)
	}
	if len(hooked) != security.MaxEvents+4 || hooked[0] != "security" {
		t.Fatalf("webhook events %v", hooked)
	}
	// The list survives a restart.
	again := &security.Monitor{Store: db}
	if got := again.Events(); len(got) != security.MaxEvents || got[0].Kind != security.SessionReplaced {
		t.Fatalf("persisted %+v", got)
	}

	// Without a monitor the fields are still there.
	s.Security = nil
	resp2, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var bare map[string]any
	json.NewDecoder(resp2.Body).Decode(&bare)
	if _, ok := bare["security_events"].([]any); !ok || bare["security_alert"] != false {
		t.Fatalf("bare status %v", bare)
	}
}

func withTokenKeystore(t *testing.T, b keystore.Backend) {
	t.Helper()
	old, oldWait := tokenBackend, tokenWait
	tokenWait = time.Millisecond
	tokenBackend = func(cfg *config.Config) keystore.Backend {
		if cfg.Encryption.Mode != config.ModeKeystore {
			return nil
		}
		return b
	}
	t.Cleanup(func() { tokenBackend, tokenWait = old, oldWait })
}

// A keystore that is down for a moment must not lead to a new plain token
// that the clients do not know.
func TestTokenWhenTheKeystoreIsDown(t *testing.T) {
	mem := keystore.NewMemory()
	withTokenKeystore(t, mem)
	cfg := config.Default()
	cfg.StoreDir = t.TempDir()
	first, err := LoadToken(cfg)
	if err != nil {
		t.Fatal(err)
	}
	mem.Err = keystore.ErrUnavailable
	if _, err := LoadToken(cfg); err == nil || !strings.Contains(err.Error(), "api.token_file_fallback") {
		t.Fatalf("keystore down: %v", err)
	}
	if _, err := ReadToken(cfg); err == nil {
		t.Fatal("ReadToken hid the keystore error")
	}
	if _, err := os.Stat(cfg.TokenFile()); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a plain token file was written")
	}
	mem.Err = nil
	if again, _ := LoadToken(cfg); again != first {
		t.Fatal("the token changed after the keystore came back")
	}
	// Only an explicit setting allows the plain file.
	mem.Err = keystore.ErrUnavailable
	cfg.API.TokenFileFallback = true
	if tok, err := LoadToken(cfg); err != nil || len(tok) != 64 || TokenSource(cfg) != cfg.TokenFile() {
		t.Fatalf("fallback: %q %v", tok, err)
	}
}

func TestTokenMovesIntoKeystore(t *testing.T) {
	mem := keystore.NewMemory()
	withTokenKeystore(t, mem)
	cfg := config.Default()
	cfg.StoreDir = filepath.Join(t.TempDir(), "store")
	os.MkdirAll(cfg.StoreDir, 0o700)
	plain := strings.Repeat("c3", 32)
	os.WriteFile(cfg.TokenFile(), []byte(plain+"\n"), 0o600)

	tok, err := LoadToken(cfg)
	if err != nil || tok != plain {
		t.Fatalf("moved token %q %v", tok, err)
	}
	if _, err := os.Stat(cfg.TokenFile()); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("the plain token file was not removed")
	}
	if v, _ := mem.Get(keystore.ItemAPIToken); string(v) != plain {
		t.Fatalf("keystore holds %q", v)
	}
	if again, _ := ReadToken(cfg); again != plain {
		t.Fatalf("read %q", again)
	}
	if !strings.HasPrefix(TokenSource(cfg), "memory") {
		t.Fatalf("source %q", TokenSource(cfg))
	}

	fresh := config.Default()
	fresh.StoreDir = filepath.Join(t.TempDir(), "store")
	mem2 := keystore.NewMemory()
	withTokenKeystore(t, mem2)
	made, err := LoadToken(fresh)
	if err != nil || len(made) != 64 {
		t.Fatalf("new token %q %v", made, err)
	}
	if _, err := os.Stat(fresh.TokenFile()); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a new token was written to a plain file")
	}

	// A broken keystore is an error, not a silent fallback to a new token.
	mem2.Err = errors.New("keystore locked")
	if _, err := LoadToken(fresh); err == nil {
		t.Fatal("broken keystore accepted")
	}

	// Mode off keeps the plain file.
	off := config.Default()
	off.Encryption.Mode = config.ModeOff
	off.StoreDir = filepath.Join(t.TempDir(), "store")
	if tok, err := LoadToken(off); err != nil || len(tok) != 64 || TokenSource(off) != off.TokenFile() {
		t.Fatalf("file token %q %v", tok, err)
	}
	if _, err := os.Stat(off.TokenFile()); err != nil {
		t.Fatal("mode off: no token file")
	}
}
