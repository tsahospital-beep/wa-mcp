package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func loadBody(t *testing.T, body string) (*Config, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return Load(path)
}

func TestEncryptionDefaultsToKeystore(t *testing.T) {
	t.Setenv("WA_MCP_ENCRYPTION_MODE", "")
	t.Setenv("WA_MCP_KEY_FILE", "")
	cfg, err := Load(filepath.Join(t.TempDir(), "missing.json"))
	if err != nil {
		t.Fatal(err)
	}
	e := cfg.Encryption
	if e.Mode != ModeKeystore || !e.Encrypted() || !e.EFS || e.PassphraseEnv != "WA_MCP_PASSPHRASE" || e.KeyFile != "" {
		t.Fatalf("defaults %+v", e)
	}
}

func TestEncryptionModesAndCompat(t *testing.T) {
	t.Setenv("WA_MCP_ENCRYPTION_MODE", "")
	t.Setenv("WA_MCP_KEY_FILE", "")
	cases := []struct {
		body, mode, warning string
	}{
		{`{"encryption": {"enabled": true, "passphrase_env": "P"}}`, ModePassphrase, "using mode passphrase"},
		{`{"encryption": {"enabled": false}}`, ModeKeystore, "set encryption.mode to off"},
		{`{"encryption": {"enabled": true, "mode": "off"}}`, ModeOff, "is ignored"},
		{`{"encryption": {"mode": "keyfile", "key_file": "secret/store.key"}}`, ModeKeyfile, ""},
	}
	for _, c := range cases {
		cfg, err := loadBody(t, c.body)
		if err != nil {
			t.Fatalf("%s: %v", c.body, err)
		}
		joined := strings.Join(cfg.Warnings, "\n")
		if cfg.Encryption.Mode != c.mode || (c.warning != "" && !strings.Contains(joined, c.warning)) {
			t.Fatalf("%s: mode %q warnings %q", c.body, cfg.Encryption.Mode, joined)
		}
		if strings.Contains(joined, "unknown key") {
			t.Fatalf("%s: %s", c.body, joined)
		}
	}
	cfg, _ := loadBody(t, `{"encryption": {"mode": "keyfile", "key_file": "secret/store.key"}}`)
	if !filepath.IsAbs(cfg.Encryption.KeyFile) || !contains(cfg.Sending.DeniedPaths, cfg.Encryption.KeyFile) {
		t.Fatalf("key file %q must be absolute and never sent (%v)", cfg.Encryption.KeyFile, cfg.Sending.DeniedPaths)
	}
	for _, bad := range []string{
		`{"encryption": {"mode": "secret"}}`,
		`{"encryption": {"mode": "keyfile"}}`,
		`{"encryption": {"mode": "passphrase", "passphrase_env": ""}}`,
	} {
		if _, err := loadBody(t, bad); err == nil || !strings.Contains(err.Error(), "encryption.") {
			t.Fatalf("accepted %s: %v", bad, err)
		}
	}

	t.Setenv("WA_MCP_ENCRYPTION_MODE", "keyfile")
	t.Setenv("WA_MCP_KEY_FILE", filepath.Join(t.TempDir(), "k"))
	cfg, err := loadBody(t, `{}`)
	if err != nil || cfg.Encryption.Mode != ModeKeyfile || cfg.Encryption.KeyFile == "" {
		t.Fatalf("env overrides: %+v %v", cfg.Encryption, err)
	}
}
