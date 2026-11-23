package sender

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

// Each account has its own limits from its config block and counts only its own sends,
// because every bridge keeps its own store.
func TestLimitsArePerAccount(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	body := `{"sending": {"enabled": true, "dedupe_window_minutes": 0, "limits": {"per_chat_per_hour": 0, "per_hour": 3, "per_day": 0}},
		"accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}, "sending": {"limits": {"per_hour": 1}}}}}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	rigFor := func(name string) *rig {
		cfg, err := config.LoadAccount(path, name)
		if err != nil {
			t.Fatal(err)
		}
		return newRig(t, func(c *config.Sending) { *c = cfg.Sending })
	}
	home, work := rigFor("default"), rigFor("work")
	send := func(r *rig, to string) error {
		_, err := r.s.Send(bg, Request{To: to, Text: "hello " + to, Human: noHuman()})
		return err
	}
	var le *LimitError
	if err := send(work, bob); err != nil {
		t.Fatal(err)
	}
	if err := send(work, "15550100003"); !errors.As(err, &le) || le.Scope != "per_hour" {
		t.Fatalf("work: want per_hour after 1 message, got %v", err)
	}
	for _, to := range []string{bob, "15550100003", "15550100004"} {
		if err := send(home, to); err != nil {
			t.Fatalf("default: %s: %v (the sends of work must not count here)", to, err)
		}
	}
	if err := send(home, "15550100005"); !errors.As(err, &le) || le.Scope != "per_hour" {
		t.Fatalf("default: want per_hour after 3 messages, got %v", err)
	}
	if len(work.tr.sent) != 1 || len(home.tr.sent) != 3 {
		t.Fatalf("sent: work %d, default %d", len(work.tr.sent), len(home.tr.sent))
	}
}
