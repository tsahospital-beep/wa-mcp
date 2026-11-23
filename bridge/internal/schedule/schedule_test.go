package schedule

import (
	"context"
	"errors"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

var bg = context.Background()

func date(y int, m time.Month, d, h, min int) time.Time {
	return time.Date(y, m, d, h, min, 0, 0, time.UTC)
}

func TestNextDailyWeekly(t *testing.T) {
	start := date(2026, 3, 30, 9, 15)
	if n, _ := Next(Daily, start, 0, time.UTC); !n.Equal(date(2026, 3, 31, 9, 15)) {
		t.Fatalf("daily: %v", n)
	}
	if n, _ := Next(Weekly, start, 0, time.UTC); !n.Equal(date(2026, 4, 6, 9, 15)) {
		t.Fatalf("weekly: %v", n)
	}
	if _, ok := Next(Once, start, 0, time.UTC); ok {
		t.Fatal("once must not repeat")
	}
}

func TestMonthlyClampsToMonthEnd(t *testing.T) {
	got := []time.Time{}
	t0 := date(2027, 1, 31, 18, 0)
	for i := 0; i < 5; i++ {
		t0, _ = Next(Monthly, t0, 31, time.UTC)
		got = append(got, t0)
	}
	want := []time.Time{date(2027, 2, 28, 18, 0), date(2027, 3, 31, 18, 0), date(2027, 4, 30, 18, 0),
		date(2027, 5, 31, 18, 0), date(2027, 6, 30, 18, 0)}
	for i := range want {
		if !got[i].Equal(want[i]) {
			t.Fatalf("step %d: %v, want %v", i, got[i], want[i])
		}
	}
	if n, _ := Next(Monthly, date(2028, 1, 31, 8, 0), 31, time.UTC); !n.Equal(date(2028, 2, 29, 8, 0)) {
		t.Fatalf("leap year: %v", n)
	}
	if n, _ := Next(Monthly, date(2026, 12, 15, 8, 0), 15, time.UTC); !n.Equal(date(2027, 1, 15, 8, 0)) {
		t.Fatalf("year rollover: %v", n)
	}
	if n, _ := Next(Monthly, date(2026, 4, 10, 8, 0), 0, time.UTC); !n.Equal(date(2026, 5, 10, 8, 0)) {
		t.Fatalf("no anchor uses the day of prev: %v", n)
	}
}

func TestAdvanceSkipsMissedRuns(t *testing.T) {
	prev := date(2026, 6, 1, 9, 0)
	now := date(2026, 6, 4, 12, 0)
	if n, _ := Advance(Daily, prev, now, 0, time.UTC); !n.Equal(date(2026, 6, 5, 9, 0)) {
		t.Fatalf("daily: %v", n)
	}
	if n, _ := Advance(Weekly, prev, now, 0, time.UTC); !n.Equal(date(2026, 6, 8, 9, 0)) {
		t.Fatalf("weekly: %v", n)
	}
	if n, _ := Advance(Monthly, date(2026, 1, 31, 9, 0), now, 31, time.UTC); !n.Equal(date(2026, 6, 30, 9, 0)) {
		t.Fatalf("monthly: %v", n)
	}
}

type fakeSend struct {
	calls []sender.Request
	err   error
}

func (f *fakeSend) send(ctx context.Context, req sender.Request) (sender.Result, error) {
	f.calls = append(f.calls, req)
	if f.err != nil {
		return sender.Result{}, f.err
	}
	return sender.Result{ID: "SENT1"}, nil
}

func openDB(t *testing.T) (*store.DB, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	return db, path
}

func TestRestartSendsMissedOnce(t *testing.T) {
	db, path := openDB(t)
	now := date(2026, 6, 4, 12, 0)
	daily, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "Daily hello",
		NextRun: date(2026, 6, 1, 9, 0).Unix(), Recurrence: Daily, CreatedAt: 1})
	once, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "One time",
		NextRun: date(2026, 6, 2, 9, 0).Unix(), Recurrence: Once, CreatedAt: 1})
	future, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "Later",
		NextRun: date(2026, 6, 10, 9, 0).Unix(), Recurrence: Once, CreatedAt: 1})

	fs := &fakeSend{}
	r := &Runner{DB: db, Send: fs.send, Now: func() time.Time { return now }, Location: time.UTC}
	if n := r.Tick(bg); n != 2 || len(fs.calls) != 2 {
		t.Fatalf("first tick handled %d, calls %d", n, len(fs.calls))
	}
	if !fs.calls[0].Scheduled {
		t.Fatal("scheduled flag not set")
	}
	if n := r.Tick(bg); n != 0 {
		t.Fatalf("second tick handled %d", n)
	}
	db.Close()

	// Restart: a new database handle and runner find nothing to resend.
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	r = &Runner{DB: db, Send: fs.send, Now: func() time.Time { return now }, Location: time.UTC}
	if n := r.Tick(bg); n != 0 || len(fs.calls) != 2 {
		t.Fatalf("after restart: handled %d, calls %d", n, len(fs.calls))
	}

	d, _ := db.GetScheduled(bg, daily)
	if d.Status != store.SchedPending || d.NextRun != date(2026, 6, 5, 9, 0).Unix() || d.LastSentAt != now.Unix() {
		t.Fatalf("daily after run: %+v", d)
	}
	o, _ := db.GetScheduled(bg, once)
	if o.Status != store.SchedSent {
		t.Fatalf("once after run: %+v", o)
	}
	f, _ := db.GetScheduled(bg, future)
	if f.Status != store.SchedPending {
		t.Fatalf("future touched: %+v", f)
	}
}

func TestQuietQueueKeepsOriginalJobAndRetryBudget(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	now := date(2026, 6, 4, 23, 0)
	next := now.Add(9 * time.Hour)
	id, err := db.AddScheduled(bg, &store.Scheduled{Target: "chat", Body: "later", NextRun: now.Unix(), QuietQueued: true})
	if err != nil {
		t.Fatal(err)
	}
	fs := &fakeSend{err: &sender.DeferUntil{End: next}}
	r := Runner{DB: db, Send: fs.send, Now: func() time.Time { return now }}
	if r.Tick(bg) != 1 || r.Tick(bg) != 0 {
		t.Fatal("unexpected repeat during quiet hours")
	}
	sc, err := db.GetScheduled(bg, id)
	if err != nil || !sc.QuietQueued || sc.Attempts != 0 || sc.Status != store.SchedPending || sc.NextRun != next.Unix() {
		t.Fatalf("deferred job %+v: %v", sc, err)
	}
}

func TestInterruptedEntriesAreNotResent(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	id, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "x", NextRun: 1, CreatedAt: 1})
	db.ClaimScheduled(bg, id) // crash happened after this point

	fs := &fakeSend{}
	ctx, cancel := context.WithCancel(bg)
	cancel()
	(&Runner{DB: db, Send: fs.send}).Run(ctx)
	if len(fs.calls) != 0 {
		t.Fatal("interrupted entry resent")
	}
	sc, _ := db.GetScheduled(bg, id)
	if sc.Status != store.SchedFailed {
		t.Fatalf("status %s", sc.Status)
	}
}

func TestInterruptedRecurringEntryMovesOn(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	now := date(2026, 6, 4, 12, 0)
	id, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "daily",
		NextRun: date(2026, 6, 4, 9, 0).Unix(), Recurrence: Daily, CreatedAt: 1})
	db.ClaimScheduled(bg, id)

	fs := &fakeSend{}
	r := &Runner{DB: db, Send: fs.send, Now: func() time.Time { return now }, Location: time.UTC}
	r.Recover(bg)
	sc, _ := db.GetScheduled(bg, id)
	if sc.Status != store.SchedPending || sc.NextRun != date(2026, 6, 5, 9, 0).Unix() || sc.Attempts != 0 {
		t.Fatalf("recurring entry after a crash: %+v", sc)
	}
	if r.Tick(bg) != 0 || len(fs.calls) != 0 {
		t.Fatal("interrupted run was sent again")
	}
}

func TestFailuresAndRetries(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	now := date(2026, 6, 4, 12, 0)
	id, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "retry me",
		NextRun: now.Unix(), CreatedAt: 1})
	fs := &fakeSend{err: errors.New("not connected to WhatsApp")}
	r := &Runner{DB: db, Send: fs.send, Now: func() time.Time { return now }, Location: time.UTC, MaxAttempts: 2}

	r.Tick(bg)
	sc, _ := db.GetScheduled(bg, id)
	if sc.Status != store.SchedPending || sc.Attempts != 1 || sc.NextRun != now.Add(time.Minute).Unix() || sc.LastError == "" {
		t.Fatalf("after first failure: %+v", sc)
	}
	now = now.Add(2 * time.Minute)
	r.Tick(bg)
	sc, _ = db.GetScheduled(bg, id)
	if sc.Status != store.SchedFailed || sc.Attempts != 2 {
		t.Fatalf("after max attempts: %+v", sc)
	}

	// A permanent error on a recurring entry skips only this run.
	weekly, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "weekly",
		NextRun: now.Unix(), Recurrence: Weekly, CreatedAt: 1})
	fs.err = &sender.PathError{Path: "/gone.jpg", Reason: "file not found"}
	r.Tick(bg)
	w, _ := db.GetScheduled(bg, weekly)
	if w.Status != store.SchedPending || w.NextRun != now.AddDate(0, 0, 7).Unix() || w.Attempts != 0 {
		t.Fatalf("weekly after permanent error: %+v", w)
	}

	// Disabled sending: nothing is attempted.
	r.Enabled = func() bool { return false }
	before := len(fs.calls)
	now = now.AddDate(0, 0, 8)
	if r.Tick(bg) != 0 || len(fs.calls) != before {
		t.Fatal("tick ran while sending is disabled")
	}
}

func TestCreateValidation(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	now := date(2026, 1, 20, 10, 0)
	sc, err := Create(bg, db, Entry{To: "+1 555 010 0002", Text: "rent reminder", At: date(2026, 1, 31, 9, 0), Recurrence: Monthly}, now, time.UTC, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if sc.ID == 0 || sc.Target != "15550100002@s.whatsapp.net" || sc.AnchorDay != 31 {
		t.Fatalf("created %+v", sc)
	}
	bad := []Entry{
		{To: "15550100002", Text: "x", At: now.Add(time.Hour), Recurrence: "hourly"},
		{To: "15550100002", At: now.Add(time.Hour)},
		{To: "15550100002", Text: "x", At: now.Add(-time.Hour)},
		{To: "15550100002", Text: "x"},
		{To: "nobody", Text: "x", At: now.Add(time.Hour)},
		{To: "15550100002", Text: "x", At: now.Add(time.Hour), MediaPath: filepath.Join(t.TempDir(), "missing.png")},
	}
	for i, e := range bad {
		if _, err := Create(bg, db, e, now, time.UTC, nil, nil); err == nil {
			t.Errorf("case %d accepted: %+v", i, e)
		}
	}

	dir := t.TempDir()
	voice := filepath.Join(dir, "note.ogg")
	sticker := filepath.Join(dir, "wave.webp")
	os.WriteFile(voice, []byte("OggS"), 0o600)
	os.WriteFile(sticker, []byte("RIFF"), 0o600)
	no := false
	v, err := Create(bg, db, Entry{To: "15550100002", MediaPath: voice, AsVoice: true, Human: &no, At: now.Add(time.Hour)}, now, time.UTC, nil, nil)
	if err != nil || v.Kind != "voice" || v.Human == nil || *v.Human {
		t.Fatalf("voice entry: %+v %v", v, err)
	}
	s, err := Create(bg, db, Entry{To: "15550100002", StickerPath: sticker, At: now.Add(time.Hour)}, now, time.UTC, nil, nil)
	if err != nil || s.Kind != "sticker" || s.MediaPath != fsutil.Resolve(sticker) {
		t.Fatalf("sticker entry: %+v %v", s, err)
	}
	if _, err := Create(bg, db, Entry{To: "15550100002", MediaPath: voice, StickerPath: sticker, At: now.Add(time.Hour)}, now, time.UTC, nil, nil); err == nil {
		t.Fatal("media plus sticker accepted")
	}
	if _, err := Create(bg, db, Entry{To: "15550100002", MediaPath: voice, At: now.Add(time.Hour)}, now, time.UTC, nil, []string{dir}); err == nil {
		t.Fatal("denied folder accepted")
	}
}

func TestShutdownFinishesInflightSend(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	now := date(2026, 6, 4, 12, 0)
	first, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "once", NextRun: now.Unix() - 10, CreatedAt: 1})
	second, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100003@s.whatsapp.net", Body: "later", NextRun: now.Unix(), CreatedAt: 1})
	ctx, cancel := context.WithCancel(bg)
	calls := 0
	send := func(sctx context.Context, req sender.Request) (sender.Result, error) {
		calls++
		cancel() // shutdown arrives during the human-like pause
		if sctx.Err() != nil {
			return sender.Result{}, sctx.Err()
		}
		return sender.Result{ID: "SENT"}, nil
	}
	r := &Runner{DB: db, Send: send, Now: func() time.Time { return now }, Location: time.UTC}
	r.Tick(ctx)
	if calls != 1 {
		t.Fatalf("sends started: %d (the second must wait for the next start)", calls)
	}
	a, _ := db.GetScheduled(bg, first)
	b, _ := db.GetScheduled(bg, second)
	if a.Status != store.SchedSent || b.Status != store.SchedPending {
		t.Fatalf("after shutdown: first=%s second=%s", a.Status, b.Status)
	}
}

func TestLastErrorHasNoFolders(t *testing.T) {
	db, _ := openDB(t)
	defer db.Close()
	now := date(2026, 6, 4, 12, 0)
	id, _ := db.AddScheduled(bg, &store.Scheduled{Target: "15550100002@s.whatsapp.net", Body: "x", NextRun: now.Unix(), CreatedAt: 1})
	fs := &fakeSend{err: errors.New(`open C:\Users\alice\private\salary review.pdf: The system cannot find the file`)}
	var logs strings.Builder
	r := &Runner{DB: db, Send: fs.send, Now: func() time.Time { return now }, Location: time.UTC, Log: log.New(&logs, "", 0)}
	r.Tick(bg)
	sc, _ := db.GetScheduled(bg, id)
	for _, s := range []string{sc.LastError, logs.String()} {
		if strings.Contains(s, "alice") || strings.Contains(s, "private") {
			t.Fatalf("folder names leaked: %q", s)
		}
	}
}
