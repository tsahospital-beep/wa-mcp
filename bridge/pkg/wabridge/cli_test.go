package wabridge

import (
	"bytes"
	"database/sql"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	_ "github.com/glebarez/go-sqlite"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

func TestVersionAndHelp(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"--version"}, &out, &errOut); code != 0 || !strings.HasPrefix(out.String(), "wa-bridge ") {
		t.Fatalf("version: %d %q", code, out.String())
	}
	out.Reset()
	if code := run([]string{"--help"}, &out, &errOut); code != 0 {
		t.Fatalf("help exit %d", code)
	}
	for _, want := range []string{"--login-preview", "import-legacy", "WA_MCP_PASSPHRASE", "sending.enabled"} {
		if !strings.Contains(errOut.String(), want) {
			t.Errorf("help lacks %q", want)
		}
	}
	if code := run([]string{"serve-now"}, &out, &errOut); code != 2 {
		t.Fatalf("unknown argument: exit %d", code)
	}
	if code := run([]string{"import-legacy"}, &out, &errOut); code != 2 {
		t.Fatalf("import-legacy without --from: exit %d", code)
	}
}

func TestImportLegacyCommand(t *testing.T) {
	dir := t.TempDir()
	old := filepath.Join(dir, "messages.db")
	db, err := sql.Open("sqlite", old)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{
		`CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)`,
		`CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
			media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER)`,
		`INSERT INTO chats VALUES ('15550100002@s.whatsapp.net', 'Bob', '2026-02-01 10:00:00+00:00')`,
		`INSERT INTO messages VALUES ('M1', '15550100002@s.whatsapp.net', '15550100002', 'hello', '2026-02-01 10:00:00+00:00', 0, '', '', '', NULL, NULL, NULL, 0)`,
	} {
		if _, err := db.Exec(s); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()

	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store"}`), 0o600)
	var out, errOut bytes.Buffer
	code := run([]string{"import-legacy", "--from", old, "--config", cfgPath}, &out, &errOut)
	if code != 0 || !strings.Contains(out.String(), "imported 1 messages") {
		t.Fatalf("exit %d, out %q, err %q", code, out.String(), errOut.String())
	}
	if _, err := os.Stat(filepath.Join(dir, "store", "wa.db")); err != nil {
		t.Fatal("wa.db not created next to the config")
	}

	// Encryption enabled without the passphrase variable is refused with a clear message.
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store2", "encryption": {"enabled": true, "passphrase_env": "WA_MCP_TEST_PASS"}}`), 0o600)
	t.Setenv("WA_MCP_TEST_PASS", "")
	errOut.Reset()
	if code := run([]string{"import-legacy", "--from", old, "--config", cfgPath}, &out, &errOut); code != 1 ||
		!strings.Contains(errOut.String(), "WA_MCP_TEST_PASS is empty") {
		t.Fatalf("missing passphrase: exit %d, %q", code, errOut.String())
	}
}

func TestImportRefusedWhileBridgeHoldsStore(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store"}`), 0o600)
	cfg, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	release, err := lockStore(cfg) // what a running bridge holds
	if err != nil {
		t.Fatal(err)
	}
	var out, errOut bytes.Buffer
	code := run([]string{"import-legacy", "--from", filepath.Join(dir, "old.db"), "--config", cfgPath}, &out, &errOut)
	if code != 1 || !strings.Contains(errOut.String(), "another bridge is using") {
		t.Fatalf("exit %d, %q", code, errOut.String())
	}
	release()
}

func TestListenWarning(t *testing.T) {
	if listenWarning("127.0.0.1:8080") != "" || listenWarning("localhost:9000") != "" || listenWarning("[::1]:8080") != "" {
		t.Fatal("loopback addresses need no warning")
	}
	all := listenWarning("0.0.0.0:8080")
	if !strings.Contains(all, "every network interface") || !strings.Contains(all, "localhost:8080") {
		t.Fatalf("unspecified: %q", all)
	}
	lan := listenWarning("192.168.1.5:8080")
	if !strings.Contains(lan, "other machines") || !strings.Contains(lan, "token") {
		t.Fatalf("lan: %q", lan)
	}
}

func TestLoginURLCommand(t *testing.T) {
	dir := t.TempDir()
	// A listener that is not the bridge: the command must fail cleanly.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close() // nothing answers on this port now
	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store", "api": {"listen": "`+addr+`"}}`), 0o600)
	var out, errOut bytes.Buffer
	if code := run([]string{"login-url", "--config", cfgPath}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "does not answer") {
		t.Fatalf("exit %d, %q", code, errOut.String())
	}

	// A running API answers with a link.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/login/url" || !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Write([]byte(`{"ok":true,"url":"http://127.0.0.1:5000/?t=abc"}`))
	}))
	defer srv.Close()
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store", "api": {"listen": "`+strings.TrimPrefix(srv.URL, "http://")+`"}}`), 0o600)
	out.Reset()
	if code := run([]string{"login-url", "--config", cfgPath}, &out, &errOut); code != 0 || strings.TrimSpace(out.String()) != "http://127.0.0.1:5000/?t=abc" {
		t.Fatalf("exit %d, out %q", code, out.String())
	}
}

func TestIsTerminalFalseForFiles(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "out")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if isTerminal(f) {
		t.Fatal("a file is not a terminal")
	}
}
