// Package sender is the outgoing pipeline: limits, dedupe, quiet hours, then
// human-like pacing (mark read, read pause, typing or recording presence) and send.
package sender

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

var (
	ErrDisabled      = errors.New("sending is disabled; set sending.enabled=true in the config")
	ErrDuplicate     = errors.New("the same message was sent to this chat a moment ago")
	ErrQuietHours    = errors.New("quiet hours are active; sending is paused")
	ErrEmpty         = errors.New("nothing to send: give text, media_path or sticker_path")
	ErrTooLarge      = errors.New("file is too large to send")
	ErrStickerFormat = errors.New("stickers must be .webp files")
)

// maxFileBytes caps attachments (WhatsApp's own limit for documents); files are streamed.
const maxFileBytes = 2 << 30

// SendTimeout bounds the final send step. It does not follow the caller's
// cancellation: once a message is handed to WhatsApp the bridge waits for the
// answer so its own records stay right.
const SendTimeout = 60 * time.Second

// Transport is the WhatsApp side (implemented by wa.Client).
type Transport interface {
	SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error)
	Upload(ctx context.Context, r io.Reader, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error)
	ChatPresence(ctx context.Context, to types.JID, state types.ChatPresence, media types.ChatPresenceMedia) error
	MarkRead(ctx context.Context, ids []string, at time.Time, chat, sender types.JID) error
	OwnJID() types.JID
}

// Clock tells the time; tests use a fake one.
type Clock interface{ Now() time.Time }

// Sleeper waits for d or until ctx ends.
type Sleeper func(ctx context.Context, d time.Duration) error

// Request is one send.
type Request struct {
	To          string   `json:"to"`
	Text        string   `json:"text"`
	MediaPath   string   `json:"media_path"`
	AsVoice     bool     `json:"as_voice"`
	StickerPath string   `json:"sticker_path"`
	ReplyTo     *Reply   `json:"reply_to"`
	Mentions    []string `json:"mentions"`
	Human       *bool    `json:"human"`
	// Scheduled marks sends from the scheduler: the time was chosen explicitly,
	// so quiet hours do not apply.
	Scheduled bool `json:"-"`
	// AsDocument sends MediaPath as a document whatever its type (set by
	// embedders through wabridge; the HTTP API does not expose it).
	AsDocument bool `json:"-"`
}

// Result describes a finished (or queued) send.
type Result struct {
	ID          string        `json:"id,omitempty"`
	IDs         []string      `json:"ids,omitempty"`
	SentAt      time.Time     `json:"-"`
	Waited      time.Duration `json:"-"`
	Queued      bool          `json:"queued,omitempty"`
	ScheduledID int64         `json:"scheduled_id,omitempty"`
}

// Sender runs the pipeline. Fields other than Cfg, DB and Transport are optional.
type Sender struct {
	Cfg       config.Sending
	DB        *store.DB
	Transport Transport
	Clock     Clock
	Sleep     Sleeper
	Rand      func() float64 // uniform in [0, 1)
	Location  *time.Location // quiet hours time zone
	// Log gets short notes; JIDs are masked and message text is never logged.
	Log *log.Logger
	// NoMarkRead turns off mark_read_before_reply (read_receipts.allow_mark_read=false).
	NoMarkRead bool

	mu       sync.Mutex // guards the maps and the limit check with its reservation
	chatLock map[string]*sync.Mutex
	lastSent map[string]time.Time
}

// Send runs the whole pipeline for one request.
func (s *Sender) Send(ctx context.Context, req Request) (Result, error) {
	if !s.Cfg.Enabled {
		return Result{}, ErrDisabled
	}
	to, err := wa.ParseTarget(req.To)
	if err != nil {
		return Result{}, err
	}
	mentions, err := ParseMentions(req.Mentions)
	if err != nil {
		return Result{}, err
	}
	// Files are opened once; the checked handles are the ones uploaded.
	var media, sticker *os.File
	defer func() {
		for _, f := range []*os.File{media, sticker} {
			if f != nil {
				f.Close()
			}
		}
	}()
	if req.MediaPath != "" {
		if media, err = OpenMedia(req.MediaPath, s.Cfg.AllowedMediaDirs, s.Cfg.DeniedPaths); err != nil {
			return Result{}, err
		}
		req.MediaPath = media.Name()
	}
	if req.StickerPath != "" {
		if sticker, err = OpenMedia(req.StickerPath, s.Cfg.AllowedMediaDirs, s.Cfg.DeniedPaths); err != nil {
			return Result{}, err
		}
		req.StickerPath = sticker.Name()
	}
	parts, err := planAs(WithMentionTags(req.Text, mentions), media, req.AsVoice, req.AsDocument, sticker)
	if err != nil {
		return Result{}, err
	}
	target := to.String()
	hash, err := s.bodyHash(ctx, req)
	if err != nil {
		return Result{}, err
	}

	release := s.lockChat(target)
	defer release()

	now := s.now()
	slots, err := s.reserve(ctx, target, hash, now, len(parts))
	if err != nil {
		return Result{}, err
	}
	sent := 0
	defer func() {
		// Give back the slots of parts that did not go out.
		if sent < len(slots) {
			_ = s.DB.ReleaseSent(context.WithoutCancel(ctx), slots[sent:])
		}
	}()

	if !req.Scheduled {
		if quiet, until := s.InQuietHours(now); quiet {
			if s.Cfg.QuietHours.Action != "queue" {
				return Result{}, ErrQuietHours
			}
			return s.queue(ctx, req, target, until, now)
		}
	}

	human := s.Cfg.HumanLike && (req.Human == nil || *req.Human)
	var waited time.Duration
	if s.Cfg.MarkReadBeforeReply && !s.NoMarkRead {
		s.markChatRead(ctx, to)
	}
	if human {
		if err := s.pauseAfterLast(ctx, target, &waited); err != nil {
			return Result{}, err
		}
		if err := s.wait(ctx, s.between(0.4, 1.5), &waited); err != nil {
			return Result{}, err
		}
	}

	reply := s.fillReply(ctx, target, req.ReplyTo)
	ci := BuildContext(reply, mentions)
	res := Result{}
	for i, p := range parts {
		if i > 0 && human {
			if err := s.wait(ctx, s.between(s.Cfg.PauseBetween[0], s.Cfg.PauseBetween[1]), &waited); err != nil {
				return res, err
			}
		}
		var pci *waE2E.ContextInfo
		if p.ctx {
			pci = ci
		}
		msg, row, voice, err := s.build(ctx, p, pci)
		if err != nil {
			return res, err
		}
		if human {
			if err := s.present(ctx, to, p, voice, &waited); err != nil {
				return res, err
			}
		}
		sendCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), SendTimeout)
		id, ts, err := s.Transport.SendMessage(sendCtx, to, msg)
		cancel()
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
				// The message may have gone out: keep its reservation so a retry
				// is refused as a duplicate instead of sending it twice.
				sent++
				return res, fmt.Errorf("send result unknown, the message may have been delivered: %w", err)
			}
			return res, fmt.Errorf("send: %w", err)
		}
		sent++
		if ts.IsZero() {
			ts = s.now()
		}
		if res.ID == "" {
			res.ID, res.SentAt = id, ts
		}
		res.IDs = append(res.IDs, id)
		s.record(context.WithoutCancel(ctx), target, id, ts, row, p.ctx, reply, mentions)
	}
	res.Waited = waited
	if len(res.IDs) < 2 {
		res.IDs = nil
	}
	return res, nil
}

func (s *Sender) logf(format string, args ...any) {
	if s.Log != nil {
		s.Log.Printf(format, args...)
	}
}
