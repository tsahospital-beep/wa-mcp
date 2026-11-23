package sender

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

func TestPauseBeforeUpload(t *testing.T) {
	r := newRig(t, nil)
	active := true
	r.s.AgentCurrent = func(string) bool { return active }
	r.s.Sleep = func(_ context.Context, d time.Duration) error {
		r.clock.Advance(d)
		active = false
		return nil
	}
	file := filepath.Join(t.TempDir(), "reply.png")
	if err := os.WriteFile(file, []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err := r.s.Send(bg, Request{To: bob, MediaPath: file, AgentRevision: "active"})
	if !errors.Is(err, ErrAgentPolicy) || len(r.tr.uploads) != 0 || len(r.tr.sent) != 0 {
		t.Fatalf("paused upload: %v, %d uploads and %d sends", err, len(r.tr.uploads), len(r.tr.sent))
	}
}

func TestCancelledBeforeHandoffReleasesReservation(t *testing.T) {
	r := newRig(t, nil)
	ctx, cancel := context.WithCancel(bg)
	defer cancel()
	sleep := r.s.Sleep
	r.s.Sleep = func(ctx context.Context, d time.Duration) error {
		r.clock.Advance(d)
		if len(r.tr.presence) > 0 {
			cancel()
		}
		return nil
	}
	req := Request{To: bob, Text: "hello"}
	if _, err := r.s.Send(ctx, req); !errors.Is(err, context.Canceled) || len(r.tr.sent) != 0 {
		t.Fatalf("cancelled send: %v", err)
	}
	r.s.Sleep = sleep
	if _, err := r.s.Send(bg, req); err != nil || len(r.tr.sent) != 1 {
		t.Fatalf("retry: %v", err)
	}
}

func TestAgentQueuedSendChecksPolicyAndLimits(t *testing.T) {
	r := newRig(t, func(c *config.Sending) {
		c.QuietHours.Enabled = true
		c.QuietHours.Start, c.QuietHours.End, c.QuietHours.Action = "11:00", "13:00", "queue"
		c.Limits.PerChatPerHour = 1
	})
	current := true
	r.s.AgentCurrent = func(rev string) bool { return current && rev == "active" }
	req := Request{To: "15550100002", Text: "Answer", AgentRevision: "active"}
	res, err := r.s.Send(bg, req)
	if err != nil || !res.Queued || len(r.tr.sent) != 0 {
		t.Fatal(res, err)
	}
	sc, err := r.db.GetScheduled(bg, res.ScheduledID)
	if err != nil {
		t.Fatal(err)
	}
	queued := RequestFromSchedule(sc)
	current = false
	if _, err = r.s.Send(bg, queued); !errors.Is(err, ErrAgentPolicy) {
		t.Fatal(err)
	}
	current = true
	var wait *DeferUntil
	if _, err = r.s.Send(bg, queued); !errors.As(err, &wait) || wait.End.Hour() != 13 {
		t.Fatalf("expected the original job to wait until 13:00: %v", err)
	}
	r.clock.Advance(time.Hour)
	if _, err = r.s.Send(bg, queued); err != nil {
		t.Fatal(err)
	}
	queued.Text = "Another answer"
	var limit *LimitError
	if _, err = r.s.Send(bg, queued); !errors.As(err, &limit) || len(r.tr.sent) != 1 {
		t.Fatal(err)
	}
}
