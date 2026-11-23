package accounts

import (
	"context"
	"database/sql"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/keystore"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/login"
)

func init() { keystore.ForStore = keystore.MemoryStores() }

const token = "0123456789abcdef0123456789abcdef"

// fakeAPI answers like a bridge: /status, and /login/url with a link or 409 when linked.
func fakeAPI(t *testing.T, linked bool) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+token {
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte(`{"ok":false,"error":"missing or wrong bearer token"}`))
			return
		}
		switch r.URL.Path {
		case "/api/v1/status":
			w.Write([]byte(`{"ok":true,"connected":true,"logged_in":true,"account":"+1 *** *** 0109"}`))
		case "/api/v1/login/url":
			if linked {
				w.WriteHeader(http.StatusConflict)
				w.Write([]byte(`{"ok":false,"error":"no login in progress; the bridge is already linked"}`))
				return
			}
			w.Write([]byte(`{"ok":true,"url":"http://127.0.0.1:5000/?t=abc"}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return strings.TrimPrefix(srv.URL, "http://")
}

func freeAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close()
	return addr
}

// setup writes a config with accounts default and work and a token file for each.
func setup(t *testing.T, defaultAddr, workAddr string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	body := `{"api": {"listen": "` + defaultAddr + `"}, "accounts": {"work": {"api": {"listen": "` + workAddr + `"}}}}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, store := range []string{filepath.Join(dir, "store"), filepath.Join(dir, "store", "work")} {
		os.MkdirAll(store, 0o700)
		os.WriteFile(filepath.Join(store, "api.token"), []byte(token+"\n"), 0o600)
	}
	return path
}

func load(t *testing.T, path, name string) *config.Config {
	t.Helper()
	cfg, err := config.LoadAccount(path, name)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func TestLoginLink(t *testing.T) {
	ctx := context.Background()
	path := setup(t, fakeAPI(t, false), fakeAPI(t, true))
	link, err := LoginLink(ctx, load(t, path, "default"))
	if err != nil || link != "http://127.0.0.1:5000/?t=abc" {
		t.Fatalf("link %q, %v", link, err)
	}
	if _, err := LoginLink(ctx, load(t, path, "work")); !errors.Is(err, login.ErrLinked) {
		t.Fatalf("linked account: %v", err)
	}
	stopped := setup(t, freeAddr(t), freeAddr(t))
	if _, err := LoginLink(ctx, load(t, stopped, "work")); !errors.Is(err, login.ErrNotRunning) || !strings.Contains(err.Error(), "does not answer") {
		t.Fatalf("stopped bridge: %v", err)
	}
}

func TestReadingStatusCreatesNoToken(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{"api": {"listen": "`+freeAddr(t)+`"}}`), 0o600)
	info := Inspect(context.Background(), load(t, path, ""))
	if info.Running || info.Linked || info.Note != "" {
		t.Fatalf("info %+v", info)
	}
	if _, err := os.Stat(filepath.Join(dir, "store", "api.token")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("Inspect created a token file")
	}
}

func TestInspect(t *testing.T) {
	path := setup(t, fakeAPI(t, false), freeAddr(t))
	def := Inspect(context.Background(), load(t, path, "default"))
	if !def.Running || !def.Connected || !def.Linked || def.Number != "+1 *** *** 0109" || def.StoreDir != "store" {
		t.Fatalf("running account: %+v", def)
	}
	// A stopped bridge: the linked device comes from session.db, opened read-only.
	work := load(t, path, "work")
	db, err := sql.Open("sqlite", work.SessionDB())
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{
		`CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, lid TEXT)`,
		`INSERT INTO whatsmeow_device VALUES ('15550100009:7@s.whatsapp.net', '200000000000009:7@lid')`,
	} {
		if _, err := db.Exec(s); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()
	info := Inspect(context.Background(), work)
	if info.Running || !info.Linked || info.Number != "+1 *** *** 0009" || info.StoreDir != "store/work" {
		t.Fatalf("stopped account: %+v", info)
	}
}

func TestWithAccounts(t *testing.T) {
	dir := t.TempDir()
	single := filepath.Join(dir, "config.json")
	os.WriteFile(single, []byte(`{}`), 0o600)
	base := login.Options{Port: 1234, OpenWindow: true}
	got := WithAccounts(load(t, single, ""), base)
	if got.Account != "" || got.Accounts != nil || got.OpenAccount != nil || got.Port != 1234 {
		t.Fatalf("one account must leave the options as they are: %+v", got)
	}

	path := setup(t, fakeAPI(t, true), fakeAPI(t, false))
	opts := WithAccounts(load(t, path, "default"), base)
	if opts.Account != "default" || strings.Join(opts.Accounts, ",") != "default,work" || !opts.OpenWindow {
		t.Fatalf("options %+v", opts)
	}
	link, err := opts.OpenAccount(context.Background(), "work")
	if err != nil || !strings.HasPrefix(link, "http://127.0.0.1:5000/") {
		t.Fatalf("open work: %q %v", link, err)
	}
	if _, err := opts.OpenAccount(context.Background(), "club"); err == nil {
		t.Fatal("an unknown account must fail")
	}
}

func TestAccountClientStopsAtRedirect(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/status" {
			t.Error("followed a redirect")
			return
		}
		http.Redirect(w, r, "/next", http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	cfg := config.Default()
	cfg.API.Listen = server.Listener.Addr().String()
	cfg.API.Token = token
	var out map[string]any
	status, _ := call(context.Background(), cfg, "GET", "/status", &out)
	if status != http.StatusTemporaryRedirect {
		t.Fatalf("status %d", status)
	}
}
