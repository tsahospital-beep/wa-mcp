package sender

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// LimitError is returned when a send limit is reached.
type LimitError struct {
	Scope string // configuration key in sending.limits
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
	if req.Action != nil {
		if req.Action.Kind == "forward" {
			// Identical text shares the same limits across ordinary sends and forwards.
			return BodyHash(key, req.Action.Value, "", "", false), nil
		}
		data, err := json.Marshal(req.Action)
		if err != nil {
			return "", err
		}
		return "action:" + BodyHash(key, string(data), "", "", false), nil
	}
	media := req.MediaPath
	if req.AsDocument {
		media += "\x00as-document" // a document send has other parts than the same file sent by type
	}
	return BodyHash(key, req.Text, media, req.StickerPath, req.AsVoice), nil
}

// ResumeWindow is how long a multi-part send that stopped after some parts
// can be resumed by the same request.
const ResumeWindow = time.Hour

// reservation holds the outbox rows of one send: the parts an earlier
// attempt of the same request sent (done) and a reserved row for each part
// to send now (slots).
type reservation struct {
	done  map[int]store.SentPart
	slots map[int]int64
	used  map[int]bool
}

func (r *reservation) mark(ctx context.Context, db *store.DB, part int, state, msgID string) error {
	id := r.slots[part]
	r.used[part] = true
	return db.MarkPart(ctx, id, state, msgID)
}

// releaseUnused gives back the slots of parts that did not go out.
func (r *reservation) releaseUnused(ctx context.Context, db *store.DB) {
	if r == nil {
		return
	}
	var ids []int64
	for part, id := range r.slots {
		if !r.used[part] {
			ids = append(ids, id)
		}
	}
	_ = db.ReleaseSent(ctx, ids)
}

// earlierParts finds what an earlier attempt of the same request sent. A
// complete earlier send inside the dedupe window is a duplicate; one outside
// it starts a new send; an incomplete one is resumed.
func (s *Sender) earlierParts(ctx context.Context, target, hash string, now time.Time, n int) (map[int]store.SentPart, error) {
	dedupe := time.Duration(s.Cfg.DedupeWindowMinutes * float64(time.Minute))
	rows, err := s.DB.SentParts(ctx, target, hash, now.Add(-max(dedupe, ResumeWindow)).Unix())
	if err != nil {
		return nil, err
	}
	done := map[int]store.SentPart{}
	var newest int64
	legacy := false
	for _, r := range rows {
		newest = max(newest, r.At)
		legacy = legacy || r.Legacy
		if _, seen := done[r.Part]; !seen && r.Part < n {
			// A reserved row of an earlier attempt may have gone out before a crash.
			done[r.Part] = r
		}
		// Part zero starts the current send. Older rows belong to earlier sends.
		if r.Part == 0 || r.Legacy {
			break
		}
	}
	if len(rows) == 0 {
		return done, nil
	}
	if legacy || len(done) == n {
		if dedupe > 0 && newest >= now.Add(-dedupe).Unix() {
			return nil, ErrDuplicate
		}
		return map[int]store.SentPart{}, nil
	}
	return done, nil
}

// reserve checks limits and dedupe and, under the same lock, writes an
// outbox row per part to send, so concurrent sends cannot pass the limits together.
func (s *Sender) reserve(ctx context.Context, target, hash string, now time.Time, n int) (*reservation, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	done, err := s.earlierParts(ctx, target, hash, now, n)
	if err != nil {
		return nil, err
	}
	var todo []int
	for i := 0; i < n; i++ {
		if _, ok := done[i]; !ok {
			todo = append(todo, i)
		}
	}
	l := s.Cfg.Limits
	if limit := l.ChatsPerIdenticalMessagePerHour; limit > 0 {
		others, err := s.DB.OtherRecipients(ctx, target, hash, now.Add(-time.Hour).Unix())
		if err != nil {
			return nil, err
		}
		if others >= limit {
			return nil, &LimitError{Scope: "chats_per_identical_message_per_hour", Limit: limit}
		}
	}
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
		if count+len(todo) > c.limit {
			return nil, &LimitError{Scope: c.scope, Limit: c.limit}
		}
	}
	ids, err := s.DB.ReserveParts(ctx, target, hash, now.Unix(), todo)
	if err != nil {
		return nil, err
	}
	r := &reservation{done: done, slots: map[int]int64{}, used: map[int]bool{}}
	for i, part := range todo {
		r.slots[part] = ids[i]
	}
	return r, nil
}
