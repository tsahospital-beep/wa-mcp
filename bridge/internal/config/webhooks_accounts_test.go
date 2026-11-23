package config

import (
	"path/filepath"
	"testing"
)

func TestWebhooksPerAccountAndDeniedStores(t *testing.T) {
	t.Setenv("WA_MCP_ENCRYPTION_MODE", "")
	path := writeConfig(t, `{"webhooks": [{"url": "http://127.0.0.1:9000/all"}],
		"encryption": {"mode": "keyfile", "key_file": "keys/store.key"},
		"accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}, "login": {"port": 18081},
			"webhooks": [{"url": "http://127.0.0.1:9000/work", "events": ["security"]}]},
			"club": {"api": {"listen": "127.0.0.1:8082"}, "login": {"port": 18082}}}}`)
	work, err := LoadAccount(path, "work")
	if err != nil {
		t.Fatal(err)
	}
	if len(work.Webhooks) != 1 || work.Webhooks[0].URL != "http://127.0.0.1:9000/work" {
		t.Fatalf("work webhooks %+v", work.Webhooks)
	}
	club, _ := LoadAccount(path, "club")
	if len(club.Webhooks) != 1 || club.Webhooks[0].URL != "http://127.0.0.1:9000/all" {
		t.Fatalf("an account without its own list inherits the top level: %+v", club.Webhooks)
	}
	dir := filepath.Dir(path)
	for _, p := range []string{filepath.Join(dir, "store"), filepath.Join(dir, "store", "work"), filepath.Join(dir, "store", "club"),
		filepath.Join(dir, "keys", "store.key")} {
		if !contains(work.Sending.DeniedPaths, p) {
			t.Errorf("%s may be sent from the work account (%v)", p, work.Sending.DeniedPaths)
		}
	}
}
