package sender

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"
)

// LimitError is returned when a send limit is reached.
type LimitError struct {
	Scope string // per_chat_per_hour | per_hour | per_day
	Limit int
}

func (e *LimitError) Error() string {
	return fmt.Sprintf("send limit reached: %s=%d", e.Scope, e.Limit)
}

// BodyHash identifies a request for dedupe: HMAC-SHA256 with the store's
// outbox key, so the log does not reveal which texts were sent.
func BodyHash(key []byte, text, mediaPath, stickerPath string, asVoice bool) string {
	m := hmac.New(sha256.New, key)
	fmt.Fprintf(m, "%s\x00%s\x00%s\x00%t", text, mediaPath, stickerPath, asVoice)
	return hex.EncodeToString(m.Sum(nil))
}

func (s *Sender) bodyHash(ctx context.Context, req Request) (string, error) {
	key, err := s.DB.OutboxKey(ctx)
	if err != nil {
		return "", err
	}
	return BodyHash(key, req.Text, req.MediaPath, req.StickerPath, req.AsVoice), nil
}

// reserve checks limits and dedupe and, under the same lock, writes n outbox
// rows, so concurrent sends cannot pass the limits together.
func (s *Sender) reserve(ctx context.Context, target, hash string, now time.Time, n int) ([]int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.Cfg.Limits
	checks := []struct {
		scope  string
		limit  int
		target string
		since  time.Duration
	}{
		{"per_chat_per_hour", l.PerChatPerHour, target, time.Hour},
		{"per_hour", l.PerHour, "", time.Hour},
		{"per_day", l.PerDay, "", 24 * time.Hour},
	}
	for _, c := range checks {
		if c.limit <= 0 {
			continue
		}
		count, err := s.DB.CountSent(ctx, c.target, now.Add(-c.since).Unix())
		if err != nil {
			return nil, err
		}
		if count+n > c.limit {
			return nil, &LimitError{Scope: c.scope, Limit: c.limit}
		}
	}
	if s.Cfg.DedupeWindowMinutes > 0 {
		since := now.Add(-time.Duration(s.Cfg.DedupeWindowMinutes * float64(time.Minute)))
		dup, err := s.DB.SentRecently(ctx, target, hash, since.Unix())
		if err != nil {
			return nil, err
		}
		if dup {
			return nil, ErrDuplicate
		}
	}
	return s.DB.ReserveSent(ctx, target, hash, now.Unix(), n)
}
