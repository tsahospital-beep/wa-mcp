package wabridge

import (
	"bytes"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func closedAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close()
	return addr
}

func TestAccountsCommand(t *testing.T) {
	const token = "0123456789abcdef0123456789abcdef"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/status" || r.Header.Get("Authorization") != "Bearer "+token {
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte(`{"ok":false}`))
			return
		}
		w.Write([]byte(`{"ok":true,"connected":true,"logged_in":true,"account":"+1 *** *** 0101"}`))
	}))
	defer srv.Close()
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	body := `{"api": {"listen": "` + strings.TrimPrefix(srv.URL, "http://") + `", "token": "` + token + `"},
		"accounts": {"work": {"api": {"listen": "` + closedAddr(t) + `"}}}}`
	os.WriteFile(cfgPath, []byte(body), 0o600)

	var out, errOut bytes.Buffer
	if code := run([]string{"accounts", "--config", cfgPath}, &out, &errOut); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 3 || !strings.HasPrefix(lines[0], "ACCOUNT") {
		t.Fatalf("table:\n%s", out.String())
	}
	if !strings.HasPrefix(lines[1], "default") || !strings.Contains(lines[1], "running, connected, linked, +1 *** *** 0101") {
		t.Fatalf("default row: %q", lines[1])
	}
	if !strings.HasPrefix(lines[2], "work") || !strings.Contains(lines[2], "store/work") || !strings.Contains(lines[2], "not running, not linked") {
		t.Fatalf("work row: %q", lines[2])
	}
	if _, err := os.Stat(filepath.Join(dir, "store", "work")); !os.IsNotExist(err) {
		t.Fatal("listing accounts must not create store folders")
	}
}

func TestAccountChoiceAndChecks(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"accounts": {"work": {"store_dir": "store/work"}}}`), 0o600)
	var out, errOut bytes.Buffer
	// run-all checks every account before it starts anything
	if code := run([]string{"run-all", "--config", cfgPath}, &out, &errOut); code != 1 || !strings.Contains(out.String(), "accounts.work.api.listen") {
		t.Fatalf("run-all: exit %d, %q", code, out.String())
	}
	if code := run([]string{"accounts", "--config", cfgPath}, &out, &errOut); code != 1 {
		t.Fatalf("accounts with a bad block: exit %d", code)
	}
	os.WriteFile(cfgPath, []byte(`{"accounts": {"work": {"api": {"listen": "`+closedAddr(t)+`"}}}}`), 0o600)
	out.Reset()
	if code := run([]string{"--account", "club", "--config", cfgPath}, &out, &errOut); code != ExitFatal || !strings.Contains(out.String(), `account "club" is not configured (accounts: default, work)`) {
		t.Fatalf("unknown account: exit %d, %q", code, out.String())
	}
	errOut.Reset()
	if code := run([]string{"login-url", "--account", "work", "--config", cfgPath}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "does not answer") {
		t.Fatalf("login-url for a stopped account: exit %d, %q", code, errOut.String())
	}
	if code := run([]string{"run-all", "extra"}, &out, &errOut); code != 2 {
		t.Fatalf("run-all with an argument: exit %d", code)
	}
}

func TestHelpMentionsAccounts(t *testing.T) {
	var out, errOut bytes.Buffer
	run([]string{"--help"}, &out, &errOut)
	for _, want := range []string{"--account NAME", "wa-bridge accounts", "wa-bridge run-all"} {
		if !strings.Contains(errOut.String(), want) {
			t.Errorf("help lacks %q", want)
		}
	}
}
