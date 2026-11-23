// Package schedule runs scheduled messages. State lives in SQLite, so entries
// survive restarts; sends go through the normal sender pipeline.
package schedule

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

// Recurrence values.
const (
	Once    = "once"
	Daily   = "daily"
	Weekly  = "weekly"
	Monthly = "monthly"
)

// ValidRecurrence reports whether rec is a known recurrence ("" means once).
func ValidRecurrence(rec string) bool {
	switch rec {
	case "", Once, Daily, Weekly, Monthly:
		return true
	}
	return false
}

func daysIn(year int, month time.Month, loc *time.Location) int {
	return time.Date(year, month+1, 0, 0, 0, 0, 0, loc).Day()
}

// Next returns the run after prev. Monthly runs use anchorDay (the day of
// month the schedule was made for), clamped to the length of each month.
func Next(rec string, prev time.Time, anchorDay int, loc *time.Location) (time.Time, bool) {
	if loc == nil {
		loc = time.Local
	}
	t := prev.In(loc)
	switch rec {
	case Daily:
		return t.AddDate(0, 0, 1), true
	case Weekly:
		return t.AddDate(0, 0, 7), true
	case Monthly:
		if anchorDay <= 0 {
			anchorDay = t.Day()
		}
		y, m := t.Year(), t.Month()+1
		if m > time.December {
			y, m = y+1, time.January
		}
		day := min(anchorDay, daysIn(y, m, loc))
		return time.Date(y, m, day, t.Hour(), t.Minute(), t.Second(), 0, loc), true
	}
	return time.Time{}, false
}

// Advance returns the first run strictly after now, skipping runs missed
// while the bridge was down.
func Advance(rec string, prev, now time.Time, anchorDay int, loc *time.Location) (time.Time, bool) {
	t, ok := Next(rec, prev, anchorDay, loc)
	for ok && !t.After(now) {
		t, ok = Next(rec, t, anchorDay, loc)
	}
	return t, ok
}

// Entry is a new schedule request.
type Entry struct {
	To          string
	Text        string
	MediaPath   string
	AsVoice     bool
	StickerPath string
	Human       *bool
	ReplyToID   string
	Mentions    []string
	At          time.Time
	Recurrence  string
}

// PastGrace is how far in the past a requested time may be.
const PastGrace = 5 * time.Minute

// Create validates and stores a new entry.
func Create(ctx context.Context, db *store.DB, e Entry, now time.Time, loc *time.Location, allowedDirs, deniedPaths []string) (*store.Scheduled, error) {
	if loc == nil {
		loc = time.Local
	}
	to, err := wa.ParseTarget(e.To)
	if err != nil {
		return nil, err
	}
	if !ValidRecurrence(e.Recurrence) {
		return nil, fmt.Errorf("recurrence must be once, daily, weekly or monthly, got %q", e.Recurrence)
	}
	if e.Recurrence == "" {
		e.Recurrence = Once
	}
	if strings.TrimSpace(e.Text) == "" && e.MediaPath == "" && e.StickerPath == "" {
		return nil, sender.ErrEmpty
	}
	kind, media, err := sender.KindOf(e.MediaPath, e.AsVoice, e.StickerPath)
	if err != nil {
		return nil, err
	}
	if media != "" {
		if media, err = sender.CheckPath(media, allowedDirs, deniedPaths); err != nil {
			return nil, err
		}
	}
	if _, err := sender.ParseMentions(e.Mentions); err != nil {
		return nil, err
	}
	if e.At.IsZero() {
		return nil, errors.New("at is required (RFC3339 time)")
	}
	if e.At.Before(now.Add(-PastGrace)) {
		return nil, errors.New("at is in the past")
	}
	sc := &store.Scheduled{
		Target: to.String(), Body: e.Text, MediaPath: media, Kind: kind, Human: e.Human, ReplyToID: e.ReplyToID, Mentions: e.Mentions,
		NextRun: e.At.Unix(), Recurrence: e.Recurrence, CreatedAt: now.Unix(),
	}
	if e.Recurrence == Monthly {
		sc.AnchorDay = e.At.In(loc).Day()
	}
	if _, err := db.AddScheduled(ctx, sc); err != nil {
		return nil, err
	}
	sc.Status = store.SchedPending
	return sc, nil
}

// SendTimeout bounds one scheduled send, human-like pauses included. The send
// does not follow shutdown: a send in progress finishes (within this time)
// so a one-time entry is not left half done.
const SendTimeout = 3 * time.Minute

// Runner checks for due entries every Interval.
type Runner struct {
	DB          *store.DB
	Send        func(ctx context.Context, req sender.Request) (sender.Result, error)
	Enabled     func() bool // sending allowed right now; nil = always
	Now         func() time.Time
	Interval    time.Duration
	Location    *time.Location
	MaxAttempts int
	Emit        func(event string, data any)
	Log         *log.Logger
}

func (r *Runner) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Runner) logf(format string, args ...any) {
	if r.Log != nil {
		r.Log.Printf(format, args...)
	}
}

func (r *Runner) emit(data map[string]any) {
	if r.Emit != nil {
		r.Emit("scheduled", data)
	}
}

// Run recovers entries interrupted by a crash, then ticks until ctx ends.
func (r *Runner) Run(ctx context.Context) {
	r.Recover(context.WithoutCancel(ctx))
	interval := r.Interval
	if interval <= 0 {
		interval = 15 * time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		r.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Recover handles entries left in "sending" by a crash. A one-time message may
// already have gone out, so it is marked failed rather than sent twice; a
// recurring one skips the interrupted run and moves to its next run.
func (r *Runner) Recover(ctx context.Context) {
	stuck, err := r.DB.ListScheduled(ctx, store.SchedSending)
	if err != nil {
		r.logf("schedule: %v", err)
		return
	}
	now := r.now()
	for _, sc := range stuck {
		u := store.ScheduleUpdate{Status: store.SchedFailed, NextRun: sc.NextRun, Attempts: sc.Attempts,
			LastError: "interrupted while sending; not retried to avoid a duplicate"}
		if next, ok := Advance(sc.Recurrence, time.Unix(sc.NextRun, 0), now, sc.AnchorDay, r.Location); ok {
			u.Status, u.NextRun, u.Attempts = store.SchedPending, next.Unix(), 0
			u.LastError = "interrupted while sending; this run was skipped"
		}
		if err := r.DB.UpdateScheduled(ctx, sc.ID, u); err != nil {
			r.logf("schedule %d: %v", sc.ID, err)
		}
	}
	if len(stuck) > 0 {
		r.logf("schedule: recovered %d entries interrupted while sending", len(stuck))
	}
}

// retryDelay grows with each failed attempt.
func retryDelay(attempt int) time.Duration {
	steps := []time.Duration{time.Minute, 5 * time.Minute, 15 * time.Minute, 30 * time.Minute, time.Hour}
	if attempt-1 < len(steps) {
		return steps[max(attempt-1, 0)]
	}
	return time.Hour
}

// permanent errors will not go away on retry.
func permanent(err error) bool {
	var pe *sender.PathError
	return errors.As(err, &pe) || errors.Is(err, sender.ErrEmpty) || errors.Is(err, sender.ErrNotOpus) ||
		errors.Is(err, wa.ErrBadTarget) || errors.Is(err, sender.ErrDuplicate) || errors.Is(err, sender.ErrTooLarge) ||
		errors.Is(err, sender.ErrStickerFormat) || errors.Is(err, sender.ErrMediaAndSticker)
}

// Tick sends every due entry once and returns how many were handled.
func (r *Runner) Tick(ctx context.Context) int {
	if r.Enabled != nil && !r.Enabled() {
		return 0
	}
	maxAttempts := r.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = 5
	}
	due, err := r.DB.DueScheduled(ctx, r.now().Unix(), 50)
	if err != nil {
		r.logf("schedule: %v", err)
		return 0
	}
	handled := 0
	db := context.WithoutCancel(ctx)
	for i := range due {
		if ctx.Err() != nil {
			break // shutting down: the rest stays pending for the next start
		}
		sc := &due[i]
		if ok, err := r.DB.ClaimScheduled(db, sc.ID); err != nil || !ok {
			continue
		}
		handled++
		sendCtx, cancel := context.WithTimeout(db, SendTimeout)
		res, err := r.Send(sendCtx, sender.RequestFromSchedule(sc))
		cancel()
		now := r.now()
		if err == nil {
			r.succeeded(db, sc, res, now)
			continue
		}
		r.failed(db, sc, err, now, maxAttempts)
	}
	return handled
}

func (r *Runner) succeeded(ctx context.Context, sc *store.Scheduled, res sender.Result, now time.Time) {
	u := store.ScheduleUpdate{Status: store.SchedSent, NextRun: sc.NextRun, LastSentAt: now.Unix()}
	if next, ok := Advance(sc.Recurrence, time.Unix(sc.NextRun, 0), now, sc.AnchorDay, r.Location); ok {
		u.Status, u.NextRun = store.SchedPending, next.Unix()
	}
	if err := r.DB.UpdateScheduled(ctx, sc.ID, u); err != nil {
		r.logf("schedule %d: %v", sc.ID, err)
	}
	r.emit(map[string]any{"id": sc.ID, "status": store.SchedSent, "target": sc.Target, "message_id": res.ID,
		"next_run": u.NextRun, "recurrence": sc.Recurrence})
}

func (r *Runner) failed(ctx context.Context, sc *store.Scheduled, err error, now time.Time, maxAttempts int) {
	attempts := sc.Attempts + 1
	msg := common.ScrubPaths(err.Error())
	u := store.ScheduleUpdate{Status: store.SchedPending, Attempts: attempts, LastError: msg}
	switch {
	case errors.Is(err, sender.ErrDisabled):
		// Sending was switched off between the check and the send; try again later.
		u.Attempts, u.NextRun = sc.Attempts, sc.NextRun
	case permanent(err) || attempts >= maxAttempts:
		if next, ok := Advance(sc.Recurrence, time.Unix(sc.NextRun, 0), now, sc.AnchorDay, r.Location); ok {
			// A recurring entry skips this run and keeps its schedule.
			u.Attempts, u.NextRun = 0, next.Unix()
		} else {
			u.Status, u.NextRun = store.SchedFailed, sc.NextRun
		}
	default:
		u.NextRun = now.Add(retryDelay(attempts)).Unix()
	}
	if uerr := r.DB.UpdateScheduled(ctx, sc.ID, u); uerr != nil {
		r.logf("schedule %d: %v", sc.ID, uerr)
	}
	r.logf("schedule %d: send failed (attempt %d): %s", sc.ID, attempts, msg)
	r.emit(map[string]any{"id": sc.ID, "status": u.Status, "target": sc.Target, "error": msg,
		"attempts": attempts, "next_run": u.NextRun})
}
