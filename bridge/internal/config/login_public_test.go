package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoginPublicValidation(t *testing.T) {
	cases := []struct {
		url, listen, want, err string
	}{
		{"", "", "", ""},
		{"https://wa.example.test/login", "0.0.0.0:8766", "https://wa.example.test/login/", ""},
		{"https://wa.example.test", "0.0.0.0:8766", "https://wa.example.test/", ""},
		{"http://127.0.0.1:9000/x/", "127.0.0.1:9000", "http://127.0.0.1:9000/x/", ""},
		{"http://wa.example.test/login/", "0.0.0.0:8766", "", "must be https"},
		{"https://wa.example.test/login/?a=1", "0.0.0.0:8766", "", "without user info"},
		{"https://user@wa.example.test/", "0.0.0.0:8766", "", "without user info"},
		{"/login/", "0.0.0.0:8766", "", "absolute URL"},
		{"https://wa.example.test/", "8766", "", "login.public.listen"},
	}
	for _, c := range cases {
		p := LoginPublic{URL: c.url, Listen: c.listen}
		err := p.validate()
		if c.err == "" {
			if err != nil || p.URL != c.want {
				t.Errorf("%q: got %q, %v", c.url, p.URL, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), c.err) {
			t.Errorf("%q: want error with %q, got %v", c.url, c.err, err)
		}
	}
}

func TestLoginPublicFromEnvAndFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{"login":{"public":{"listen":"0.0.0.0:8766"}}}`), 0o600)
	t.Setenv("WA_MCP_LOGIN_PUBLIC_URL", "https://wa.example.test/login")
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.Login.Public.Enabled() || cfg.Login.Public.URL != "https://wa.example.test/login/" || cfg.Login.Public.Listen != "0.0.0.0:8766" {
		t.Fatalf("login.public: %+v", cfg.Login.Public)
	}
	if len(cfg.Warnings) != 0 {
		t.Fatalf("warnings: %v", cfg.Warnings)
	}
	t.Setenv("WA_MCP_LOGIN_PUBLIC_URL", "http://wa.example.test/")
	if _, err := Load(path); err == nil {
		t.Fatal("plain http on a public host must be refused")
	}
}

func TestServerSectionIsPythonOnly(t *testing.T) {
	if keys := UnknownKeys([]byte(`{"server":{"listen":"127.0.0.1:8765","oauth":{"enabled":true}}}`)); len(keys) != 0 {
		t.Fatalf("server keys reported as unknown: %v", keys)
	}
}
