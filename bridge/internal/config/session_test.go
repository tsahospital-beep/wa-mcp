package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSessionFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	cfg, err := Load(path)
	if err != nil || cfg.SessionDB() != filepath.Join(dir, "store", "session.db") {
		t.Fatalf("default: %v %v", cfg, err)
	}
	os.WriteFile(path, []byte(`{"session_file": "whatsapp.db"}`), 0o600)
	cfg, err = Load(path)
	if err != nil || cfg.SessionDB() != filepath.Join(dir, "store", "whatsapp.db") || len(cfg.Warnings) != 0 {
		t.Fatalf("whatsapp.db: %v %v", cfg, err)
	}
	for _, bad := range []string{`../x.db`, `sub/x.db`, `C:x.db`, `wa.db`, `API.TOKEN`, `..`} {
		os.WriteFile(path, []byte(`{"session_file": "`+bad+`"}`), 0o600)
		if _, err := Load(path); err == nil || !strings.Contains(err.Error(), "session_file") {
			t.Errorf("%q accepted: %v", bad, err)
		}
	}
	if w := UnknownKeyWarning("/x/config.json", "legacy.port"); w != `config /x/config.json: unknown key "legacy.port" is ignored` {
		t.Fatalf("warning text %q", w)
	}
}
