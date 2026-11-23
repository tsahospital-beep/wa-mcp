package sender

import (
	"context"
	"math/rand/v2"
	"time"
	"unicode/utf8"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func realSleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Sender) now() time.Time {
	if s.Clock == nil {
		return time.Now()
	}
	return s.Clock.Now()
}

func (s *Sender) rnd() float64 {
	if s.Rand == nil {
		return rand.Float64()
	}
	return s.Rand()
}

func (s *Sender) wait(ctx context.Context, d time.Duration, total *time.Duration) error {
	if d <= 0 {
		return nil
	}
	sleep := s.Sleep
	if sleep == nil {
		sleep = realSleep
	}
	*total += d
	return sleep(ctx, d)
}

func seconds(f float64) time.Duration { return time.Duration(f * float64(time.Second)) }

func (s *Sender) between(lo, hi float64) time.Duration {
	if hi <= lo {
		return seconds(lo)
	}
	return seconds(lo + (hi-lo)*s.rnd())
}

// pauseAfterLast keeps the configured gap after the previous message to the same chat.
func (s *Sender) pauseAfterLast(ctx context.Context, target string, waited *time.Duration) error {
	s.mu.Lock()
	last := s.chats[target].lastSent
	s.mu.Unlock()
	if last.IsZero() {
		return nil
	}
	gap := s.between(s.Cfg.PauseBetween[0], s.Cfg.PauseBetween[1])
	if elapsed := s.now().Sub(last); elapsed < gap {
		return s.wait(ctx, gap-elapsed, waited)
	}
	return nil
}

// TypingTime is clamp(chars / cps, min, max) * (1 +/- jitter).
func (s *Sender) TypingTime(chars int) time.Duration {
	return s.jittered(float64(chars) / s.Cfg.TypingCharsPerSecond)
}

func (s *Sender) jittered(sec float64) time.Duration {
	sec = max(s.Cfg.TypingMinSeconds, min(sec, s.Cfg.TypingMaxSeconds))
	sec *= 1 + s.Cfg.Jitter*(2*s.rnd()-1)
	return seconds(sec)
}

// InQuietHours reports whether t falls in the configured window and when it ends.
func (s *Sender) InQuietHours(t time.Time) (bool, time.Time) {
	q := s.Cfg.QuietHours
	if !q.Enabled {
		return false, time.Time{}
	}
	start, err1 := config.ParseClock(q.Start)
	end, err2 := config.ParseClock(q.End)
	if err1 != nil || err2 != nil || start == end {
		return false, time.Time{}
	}
	loc := s.Location
	if loc == nil {
		loc = time.Local
	}
	// Compare wall-clock minutes, not time since midnight: on daylight saving
	// days a day has 23 or 25 hours.
	t = t.In(loc)
	at := t.Hour()*60 + t.Minute()
	startM, endM := int(start/time.Minute), int(end/time.Minute)
	clock := func(dayOffset, minutes int) time.Time {
		return time.Date(t.Year(), t.Month(), t.Day()+dayOffset, minutes/60, minutes%60, 0, 0, loc)
	}
	if startM < endM {
		if at >= startM && at < endM {
			return true, clock(0, endM)
		}
		return false, time.Time{}
	}
	// The window wraps past midnight, e.g. 23:00-08:00.
	if at >= startM {
		return true, clock(1, endM)
	}
	if at < endM {
		return true, clock(0, endM)
	}
	return false, time.Time{}
}

// present shows typing (or recording for voice notes), waits, then pauses.
func (s *Sender) present(ctx context.Context, to types.JID, p part, voice VoiceInfo, waited *time.Duration) error {
	media := types.ChatPresenceMediaText
	var d time.Duration
	switch p.kind {
	case store.KindVoice:
		media = types.ChatPresenceMediaAudio
		d = s.jittered(float64(voice.Seconds))
	case store.KindSticker:
		d = s.jittered(0)
	default:
		d = s.TypingTime(utf8.RuneCountInString(p.text))
	}
	if err := s.Transport.ChatPresence(ctx, to, types.ChatPresenceComposing, media); err != nil {
		s.logf("presence: %v", err)
	}
	if err := s.wait(ctx, d, waited); err != nil {
		return err
	}
	if err := s.Transport.ChatPresence(ctx, to, types.ChatPresencePaused, media); err != nil {
		s.logf("presence: %v", err)
	}
	return nil
}
