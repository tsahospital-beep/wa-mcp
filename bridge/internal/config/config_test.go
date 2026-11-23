package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMissingFileGivesSafeDefaults(t *testing.T) {
	dir := t.TempDir()
	cfg, err := Load(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Sending.Enabled || !cfg.Sending.RequireConfirm || cfg.ReadReceipts.AutoMarkRead || cfg.Encryption.Enabled || len(cfg.Webhooks) != 0 {
		t.Fatalf("unsafe defaults: %+v", cfg)
	}
	if cfg.API.Listen != "127.0.0.1:8080" {
		t.Fatalf("listen %s", cfg.API.Listen)
	}
	if cfg.StoreDir != filepath.Join(dir, "store") || cfg.MessagesDB() != filepath.Join(dir, "store", "wa.db") {
		t.Fatalf("store dir %s", cfg.StoreDir)
	}
	if !cfg.ReadReceipts.AllowMarkRead || cfg.Media.MaxDownloadMB != 512 || cfg.MaxDownloadBytes() != 512<<20 {
		t.Fatalf("read/media defaults: %+v %+v", cfg.ReadReceipts, cfg.Media)
	}
	if len(cfg.Warnings) != 0 {
		t.Fatalf("warnings without a file: %v", cfg.Warnings)
	}
}

func TestDefaultMediaDirsAndDenyList(t *testing.T) {
	dir := t.TempDir()
	cfg, err := Load(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{filepath.Join(dir, "downloads"), filepath.Join(dir, "exports")}
	got := cfg.Sending.AllowedMediaDirs
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("default media dirs %v, want %v", got, want)
	}
	denied := cfg.Sending.DeniedPaths
	if len(denied) != 2 || denied[0] != cfg.StoreDir || denied[1] != cfg.File {
		t.Fatalf("deny list %v", denied)
	}
}

func TestAllowedMediaDirsAddToDefaults(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{"sending": {"allowed_media_dirs": ["share-out", "downloads"]}}`), 0o600)
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{filepath.Join(dir, "downloads"), filepath.Join(dir, "exports"), filepath.Join(dir, "share-out")}
	got := cfg.Sending.AllowedMediaDirs
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("allowed dirs %v, want %v (defaults first, no duplicates)", got, want)
	}
}

func TestHomeExpansion(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{"store_dir": "~/wa/store", "downloads_dir": "~", "exports_dir": "~other"}`), 0o600)
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StoreDir != filepath.Join(home, "wa", "store") || cfg.DownloadsDir != home {
		t.Fatalf("~ not expanded: %s %s", cfg.StoreDir, cfg.DownloadsDir)
	}
	if cfg.ExportsDir != filepath.Join(dir, "~other") {
		t.Fatalf("~name must stay relative: %s", cfg.ExportsDir)
	}
}

func TestPythonOnlyKeysAccepted(t *testing.T) {
	doc := `{"sending": {"allow_confirm_flag": true, "allow_confirm_flags": true}}`
	if got := strings.Join(UnknownKeys([]byte(doc)), ","); got != "sending.allow_confirm_flags" {
		t.Fatalf("unknown keys %q: the Python-only key must pass, a typo must not", got)
	}
}

func TestInsightSectionIsKnown(t *testing.T) {
	if keys := UnknownKeys([]byte(`{"insight": {"anything": 1}, "mentions": {"watch": []}}`)); len(keys) != 0 {
		t.Fatalf("python sections reported: %v", keys)
	}
}

func TestUnknownKeysAndBOM(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	doc := `{"store_dir": "s", "stor_dir": "typo", "login": {"language": "en"},
		"sending": {"limits": {"per_week": 3}}, "webhooks": [{"url": "http://127.0.0.1:9/x", "secrett": 1}],
		"transcription": {"anything": true}, "people": {"aliases": {}}}`
	os.WriteFile(path, append([]byte("\xef\xbb\xbf"), doc...), 0o600)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("BOM must be accepted: %v", err)
	}
	want := "login.language,sending.limits.per_week,stor_dir,webhooks[0].secrett"
	if got := strings.Join(UnknownKeys([]byte(doc)), ","); got != want {
		t.Fatalf("unknown keys %s, want %s", got, want)
	}
	if len(cfg.Warnings) != 4 || !strings.Contains(cfg.Warnings[0], "is ignored") {
		t.Fatalf("warnings %v", cfg.Warnings)
	}
	if cfg.StoreDir != filepath.Join(dir, "s") {
		t.Fatalf("value after BOM: %s", cfg.StoreDir)
	}
}

func TestFileValuesAndRelativePaths(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{
		"store_dir": "data/store",
		"api": {"listen": "127.0.0.1:9191"},
		"sending": {"enabled": true, "limits": {"per_day": 10}, "allowed_media_dirs": ["outbox"],
		            "quiet_hours": {"enabled": true, "start": "22:30", "end": "07:15"}},
		"webhooks": [{"url": "http://127.0.0.1:9000/hook", "events": ["message", "call"], "secret_env": "HOOK"}],
		"transcription": {"backend": "faster-whisper"}
	}`), 0o600)
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StoreDir != filepath.Join(dir, "data", "store") || cfg.API.Listen != "127.0.0.1:9191" {
		t.Fatalf("values: %+v", cfg)
	}
	s := cfg.Sending
	if !s.Enabled || s.Limits.PerDay != 10 || s.Limits.PerHour != 60 || s.TypingCharsPerSecond != 7 {
		t.Fatalf("sending merge: %+v", s)
	}
	if len(s.AllowedMediaDirs) != 3 || s.AllowedMediaDirs[2] != filepath.Join(dir, "outbox") {
		t.Fatalf("allowed dirs: %v", s.AllowedMediaDirs)
	}
	if s.QuietHours.Action != "queue" || len(cfg.Webhooks) != 1 {
		t.Fatalf("quiet hours/webhooks: %+v %+v", s.QuietHours, cfg.Webhooks)
	}
}

func TestEnvOverrides(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("WA_MCP_API_LISTEN", "127.0.0.1:7000")
	t.Setenv("WA_MCP_SENDING_ENABLED", "true")
	t.Setenv("WA_MCP_STORE_DIR", "elsewhere")
	cfg, err := Load(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.API.Listen != "127.0.0.1:7000" || !cfg.Sending.Enabled || cfg.StoreDir != filepath.Join(dir, "elsewhere") {
		t.Fatalf("env: %+v", cfg)
	}
	t.Setenv("WA_MCP_SENDING_ENABLED", "maybe")
	if _, err := Load(filepath.Join(dir, "config.json")); err == nil {
		t.Fatal("bad boolean accepted")
	}
}

func TestValidation(t *testing.T) {
	bad := []string{
		`{"api": {"listen": "8080"}}`,
		`{"sending": {"jitter": 1.5}}`,
		`{"media": {"max_download_mb": 0}}`,
		`{"sending": {"pause_between_messages_seconds": [3, 1]}}`,
		`{"sending": {"quiet_hours": {"start": "25:00"}}}`,
		`{"sending": {"quiet_hours": {"action": "drop"}}}`,
		`{"sending": {"typing_min_seconds": 5, "typing_max_seconds": 2}}`,
		`{"webhooks": [{"url": "ftp://example.org"}]}`,
		`{"webhooks": [{"url": "http://127.0.0.1:1/x", "events": ["everything"]}]}`,
		`{"encryption": {"enabled": true, "passphrase_env": ""}}`,
		`{not json`,
	}
	for _, body := range bad {
		path := filepath.Join(t.TempDir(), "config.json")
		os.WriteFile(path, []byte(body), 0o600)
		if _, err := Load(path); err == nil {
			t.Errorf("accepted: %s", body)
		} else if !strings.Contains(err.Error(), "config.json") {
			t.Errorf("error should name the file: %v", err)
		}
	}
}

func TestParseClock(t *testing.T) {
	d, err := ParseClock("07:45")
	if err != nil || d != 7*time.Hour+45*time.Minute {
		t.Fatalf("%v %v", d, err)
	}
	if _, err := ParseClock("7pm"); err == nil {
		t.Fatal("bad clock accepted")
	}
}

func TestResolvePath(t *testing.T) {
	t.Setenv("WA_MCP_CONFIG", "/etc/wa-mcp/config.json")
	if ResolvePath("x.json") != "x.json" || ResolvePath("") != "/etc/wa-mcp/config.json" {
		t.Fatal("resolve order")
	}
	t.Setenv("WA_MCP_CONFIG", "")
	if ResolvePath("") != "config.json" {
		t.Fatal("default path")
	}
}

func TestLoginDeviceSettings(t *testing.T) {
	cfg := Default()
	if cfg.Login.DeviceName != "WA MCP" || cfg.Login.DevicePlatform != "desktop" {
		t.Fatal(cfg.Login)
	}
	for _, platform := range []string{"desktop", "chrome", "firefox", "safari", "edge", "unknown", "invalid"} {
		cfg.Login.DevicePlatform = platform
		if err := cfg.Validate(); (err != nil) != (platform == "invalid") {
			t.Fatalf("%s: %v", platform, err)
		}
	}
	cfg.Login.DevicePlatform = "desktop"
	cfg.Login.DeviceName = " "
	if err := cfg.Validate(); err == nil {
		t.Fatal("empty name accepted")
	}
}
