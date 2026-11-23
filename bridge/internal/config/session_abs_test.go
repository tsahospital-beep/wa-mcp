package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAbsoluteSessionFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	session := filepath.Join(dir, "session-volume", "session.db")
	body, _ := json.Marshal(map[string]any{"session_file": session})
	os.WriteFile(path, body, 0o600)
	cfg, err := Load(path)
	if err != nil || cfg.SessionDB() != session {
		t.Fatalf("absolute: %q %v", cfg.SessionDB(), err)
	}
	for _, p := range []string{session, session + "-wal", session + "-shm"} {
		if !contains(cfg.Sending.DeniedPaths, p) {
			t.Errorf("%s may be sent", p)
		}
	}
	for _, inside := range []string{filepath.Join(dir, "downloads", "s.db"), filepath.Join(dir, "exports", "s.db"),
		filepath.Join(dir, "media", "s.db")} {
		body, _ := json.Marshal(map[string]any{"session_file": inside, "sending": map[string]any{"allowed_media_dirs": []string{"media"}}})
		os.WriteFile(path, body, 0o600)
		if _, err := Load(path); err == nil || !strings.Contains(err.Error(), "session_file") {
			t.Errorf("%s accepted: %v", inside, err)
		}
	}
}
