package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestImportDefaults(t *testing.T) {
	dir := t.TempDir()
	cfg, err := Load(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Import.MaxMB != 1024 || cfg.MaxImportBytes() != 1<<30 || len(cfg.Import.AllowedDirs) != 0 || len(cfg.Import.OmittedPhrases) != 0 {
		t.Fatalf("defaults: %+v", cfg.Import)
	}
	dirs := cfg.ImportDirs()
	if len(dirs) != 2 || dirs[0] != cfg.DownloadsDir || dirs[1] != cfg.ExportsDir {
		t.Fatalf("import folders %v", dirs)
	}
}

func TestImportSettings(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	body := `{"import": {"allowed_dirs": ["exports-in", "downloads", " "], "max_mb": 64, "omitted_phrases": ["<Foto weggelassen>", " "]}}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Warnings) != 0 {
		t.Fatalf("warnings %v", cfg.Warnings)
	}
	dirs := cfg.ImportDirs()
	want := []string{filepath.Join(dir, "downloads"), filepath.Join(dir, "exports"), filepath.Join(dir, "exports-in")}
	if len(dirs) != 3 || dirs[0] != want[0] || dirs[1] != want[1] || dirs[2] != want[2] {
		t.Fatalf("import folders %v, want %v", dirs, want)
	}
	if cfg.MaxImportBytes() != 64<<20 || len(cfg.Import.OmittedPhrases) != 1 {
		t.Fatalf("settings %+v", cfg.Import)
	}
}

func TestImportMaxMustBePositive(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	if err := os.WriteFile(path, []byte(`{"import": {"max_mb": 0}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil || !strings.Contains(err.Error(), "import.max_mb") {
		t.Fatalf("error %v", err)
	}
}
