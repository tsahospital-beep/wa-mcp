package config

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestNoAccountsKeepsTheTopLevelSettings(t *testing.T) {
	body := `{"store_dir": "data", "api": {"listen": "127.0.0.1:9100"}, "sending": {"enabled": true}}`
	path := writeConfig(t, body)
	plain, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	named, err := LoadAccount(path, DefaultAccount)
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Dir(path)
	if plain.Account != DefaultAccount || plain.StoreDir != filepath.Join(dir, "data") || plain.API.Listen != "127.0.0.1:9100" {
		t.Fatalf("default account: %s %s %s", plain.Account, plain.StoreDir, plain.API.Listen)
	}
	if !reflect.DeepEqual(plain, named) {
		t.Fatal(`Load and LoadAccount(path, "default") differ`)
	}
	if got := plain.AccountNames(); len(got) != 1 || got[0] != DefaultAccount {
		t.Fatalf("names %v", got)
	}
	if len(plain.Warnings) != 0 {
		t.Fatalf("warnings %v", plain.Warnings)
	}
	if _, err := LoadAccount(path, "work"); err == nil || !strings.Contains(err.Error(), `account "work" is not configured`) {
		t.Fatalf("unknown account: %v", err)
	}
}

func TestAccountInheritsAndOverrides(t *testing.T) {
	clubDownloads := filepath.Join(t.TempDir(), "club")
	path := writeConfig(t, strings.ReplaceAll(`{
		"store_dir": "store",
		"sending": {"enabled": true, "limits": {"per_chat_per_hour": 7, "per_hour": 30, "per_day": 100}},
		"read_receipts": {"auto_mark_read": true},
		"accounts": {
			"default": {},
			"work": {
				"api": {"listen": "127.0.0.1:8081"},
				"login": {"port": 8771},
				"sending": {"limits": {"per_hour": 5}, "allow_confirm_flag": true, "quiet_hours": {"enabled": true}}
			},
			"club": {"store_dir": "other/club-store", "downloads_dir": "ABS", "api": {"listen": "127.0.0.1:8082"}}
		}
	}`, "ABS", filepath.ToSlash(clubDownloads)))
	dir := filepath.Dir(path)
	def, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	work, err := LoadAccount(path, "work")
	if err != nil {
		t.Fatal(err)
	}
	club, err := LoadAccount(path, "club")
	if err != nil {
		t.Fatal(err)
	}
	if got := def.AccountNames(); strings.Join(got, ",") != "default,club,work" {
		t.Fatalf("names %v", got)
	}
	if len(def.Warnings) != 0 {
		t.Fatalf("allow_confirm_flag inside an account is a Python key: %v", def.Warnings)
	}
	// default: the top level as before
	if def.StoreDir != filepath.Join(dir, "store") || def.API.Listen != "127.0.0.1:8080" || def.Sending.Limits.PerHour != 30 {
		t.Fatalf("default: %s %s %+v", def.StoreDir, def.API.Listen, def.Sending.Limits)
	}
	// work: own folders below the top-level ones, own address and limits, the rest inherited
	if work.Account != "work" || work.StoreDir != filepath.Join(dir, "store", "work") ||
		work.DownloadsDir != filepath.Join(dir, "downloads", "work") || work.ExportsDir != filepath.Join(dir, "exports", "work") {
		t.Fatalf("work folders: %s %s %s", work.StoreDir, work.DownloadsDir, work.ExportsDir)
	}
	l := work.Sending.Limits
	if work.API.Listen != "127.0.0.1:8081" || work.Login.Port != 8771 || l.PerHour != 5 || l.PerChatPerHour != 7 || l.PerDay != 100 {
		t.Fatalf("work overrides: %s %d %+v", work.API.Listen, work.Login.Port, l)
	}
	if !work.Sending.Enabled || !work.Sending.QuietHours.Enabled || work.Sending.QuietHours.Start != "23:00" || !work.ReadReceipts.AutoMarkRead {
		t.Fatalf("work inherits: %+v", work.Sending.QuietHours)
	}
	if def.Sending.QuietHours.Enabled || def.Login.Port != 0 {
		t.Fatal("an account block changed the default account")
	}
	want := []string{work.DownloadsDir, work.ExportsDir}
	if !reflect.DeepEqual(work.Sending.AllowedMediaDirs, want) || work.Sending.DeniedPaths[0] != work.StoreDir {
		t.Fatalf("work media dirs %v denied %v", work.Sending.AllowedMediaDirs, work.Sending.DeniedPaths)
	}
	// club: explicit folders win
	if club.StoreDir != filepath.Join(dir, "other", "club-store") || club.DownloadsDir != clubDownloads ||
		club.ExportsDir != filepath.Join(dir, "exports", "club") {
		t.Fatalf("club folders: %s %s %s", club.StoreDir, club.DownloadsDir, club.ExportsDir)
	}
}

func TestAccountChecks(t *testing.T) {
	cases := map[string]string{
		`{"accounts": {"work": {}}}`:                                                              "accounts.work.api.listen: set an address",
		`{"accounts": {"work": {"api": {"listen": "localhost:8080"}}}}`:                           "port 8080 is already used by account default",
		`{"accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}, "store_dir": "store"}}}`:     "is already used by account default",
		`{"login": {"port": 8770}, "accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}}}}`:  "accounts.work.login.port: 8770",
		`{"accounts": {"Work Phone": {"api": {"listen": "127.0.0.1:8081"}}}}`:                     "not a valid account name",
		`{"accounts": {"work": {"api": {"listen": 8081}}}}`:                                       "cannot unmarshal",
		`{"accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}, "sending": {"jitter": 3}}}}`: "",
	}
	for body, want := range cases {
		path := writeConfig(t, body)
		_, err := Load(path)
		if want == "" {
			// a bad value inside another account only stops that account
			if err != nil {
				t.Fatalf("%s: default account: %v", body, err)
			}
			if _, err := LoadAccount(path, "work"); err == nil || !strings.Contains(err.Error(), "sending.jitter") {
				t.Fatalf("%s: work: %v", body, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("%s: got %v, want %q", body, err, want)
		}
	}
}

func TestUnknownKeysInsideAccounts(t *testing.T) {
	path := writeConfig(t, `{"accounts": {"work": {"api": {"listen": "127.0.0.1:8081", "token": "x"},
		"webhooks": [], "sending": {"limits": {"per_hour": 2, "per_minute": 1}}}}}`)
	cfg, err := LoadAccount(path, "work")
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(cfg.Warnings, "\n")
	for _, key := range []string{"accounts.work.api.token", "accounts.work.sending.limits.per_minute"} {
		if !strings.Contains(got, `"`+key+`"`) {
			t.Errorf("no warning for %s in %q", key, got)
		}
	}
	if cfg.Sending.Limits.PerHour != 2 || cfg.API.Token != "" {
		t.Fatalf("limits %+v token %q", cfg.Sending.Limits, cfg.API.Token)
	}
}

func TestEnvOverridesTheTopLevelOnly(t *testing.T) {
	path := writeConfig(t, `{"accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}}}}`)
	t.Setenv("WA_MCP_API_LISTEN", "127.0.0.1:9200")
	t.Setenv("WA_MCP_STORE_DIR", "data")
	def, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	work, err := LoadAccount(path, "work")
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Dir(path)
	if def.API.Listen != "127.0.0.1:9200" || work.API.Listen != "127.0.0.1:8081" {
		t.Fatalf("listen: %s %s", def.API.Listen, work.API.Listen)
	}
	if def.StoreDir != filepath.Join(dir, "data") || work.StoreDir != filepath.Join(dir, "data", "work") {
		t.Fatalf("store: %s %s", def.StoreDir, work.StoreDir)
	}
}

func TestStripAccount(t *testing.T) {
	for in, want := range map[string]string{
		"accounts.work.sending.allow_confirm_flag": "sending.allow_confirm_flag",
		"sending.allow_confirm_flag":               "sending.allow_confirm_flag",
		"accounts.work":                            "accounts.work",
	} {
		if got := stripAccount(in); got != want {
			t.Errorf("stripAccount(%q) = %q, want %q", in, got, want)
		}
	}
}
