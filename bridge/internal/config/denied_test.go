package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

func TestOtherAccountsFoldersAreNeverSent(t *testing.T) {
	t.Setenv("WA_MCP_ENCRYPTION_MODE", "")
	pass := filepath.Join(t.TempDir(), "pass")
	t.Setenv("WA_MCP_PASSPHRASE_FILE", pass)
	path := writeConfig(t, `{"accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}, "login": {"port": 18081}}}}`)
	def, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Dir(path)
	for _, p := range []string{filepath.Join(dir, "downloads", "work"), filepath.Join(dir, "exports", "work"),
		filepath.Join(dir, "store", "work"), pass} {
		if !contains(def.Sending.DeniedPaths, p) {
			t.Errorf("default may send %s", p)
		}
	}
	if got := def.OtherAccountFolders(); len(got) != 3 {
		t.Fatalf("other folders %v", got)
	}
	work, _ := LoadAccount(path, "work")
	if contains(work.Sending.DeniedPaths, filepath.Join(dir, "downloads", "work")) {
		t.Fatal("an account cannot send from its own downloads folder")
	}

	body, _ := json.Marshal(map[string]any{"session_file": filepath.Join(dir, "session", "session.db"),
		"accounts": map[string]any{"work": map[string]any{"api": map[string]any{"listen": "127.0.0.1:8081"}}}})
	os.WriteFile(path, body, 0o600)
	if _, err := Load(path); err == nil || !strings.Contains(err.Error(), "shared by all accounts") {
		t.Fatalf("absolute session_file with accounts: %v", err)
	}
}

func TestNestedAccountCanReadOnlyOwnMedia(t *testing.T) {
	path := writeConfig(t, `{"store_dir":"private","accounts":{"work":{"api":{"listen":"127.0.0.1:0"}}}}`)
	cfg, err := LoadAccount(path, "work")
	if err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{cfg.DownloadsDir, filepath.Dir(cfg.DownloadsDir)} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		file := filepath.Join(dir, "note.txt")
		if err := os.WriteFile(file, []byte("note"), 0o600); err != nil {
			t.Fatal(err)
		}
		f, reason := fsutil.OpenRegular(file, nil, cfg.Sending.DeniedPaths, cfg.Sending.ForeignMedia)
		if f != nil {
			f.Close()
		}
		if (reason == "") != (dir == cfg.DownloadsDir) {
			t.Fatalf("open %s: %s", dir, reason)
		}
	}
}

func TestPythonSecretFilesAreDenied(t *testing.T) {
	if _, err := Load(writeConfig(t, `{"ai":{"api_key_env":42}}`)); err == nil {
		t.Fatal("invalid credential setting accepted")
	}
	dir := t.TempDir()
	names := []string{"CUSTOM_AI", "WA_MCP_STT_API_KEY", "WA_MCP_ADMIN_PASSWORD", "WA_MCP_SERVER_PEPPER"}
	for _, name := range names {
		t.Setenv(name+"_FILE", filepath.Join(dir, name))
	}
	cfg, err := Load(writeConfig(t, `{"ai":{"api_key_env":"CUSTOM_AI"}}`))
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range names {
		if !contains(cfg.Sending.DeniedPaths, filepath.Join(dir, name)) {
			t.Errorf("missing %s", name)
		}
	}
}
