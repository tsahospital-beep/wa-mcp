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
	"slices"
	"strings"
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
	ErrAgentPolicy   = errors.New("agent paused or config changed")
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
	Action        *wa.MessageAction `json:"-"`
	AgentRevision string            `json:"agent_revision,omitempty"`
	To            string            `json:"to"`
	Text          string            `json:"text"`
	MediaPath     string            `json:"media_path"`
	AsVoice       bool              `json:"as_voice"`
	StickerPath   string            `json:"sticker_path"`
	ReplyTo       *Reply            `json:"reply_to"`
	Mentions      []string          `json:"mentions"`
	Human         *bool             `json:"human"`
	// Scheduled marks sends from the scheduler: the time was chosen explicitly,
	// so quiet hours do not apply.
	Scheduled   bool `json:"-"`
	quietQueued bool
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
	// Resumed is set when an earlier attempt of the same request had sent some parts.
	Resumed bool `json:"resumed,omitempty"`
}

func (r *Result) add(id string, at time.Time) {
	if r.ID == "" {
		r.ID, r.SentAt = id, at
	}
	r.IDs = append(r.IDs, id)
}

// Sender runs the pipeline. Fields other than Cfg, DB and Transport are optional.
type Sender struct {
	// BuildAction supplies the protocol builder for message actions.
	BuildAction  func(wa.MessageAction, types.JID, types.JID) (*waE2E.Message, error)
	AgentCurrent func(string) bool
	Cfg          config.Sending
	DB           *store.DB
	Transport    Transport
	Clock        Clock
	Sleep        Sleeper
	Rand         func() float64 // uniform in [0, 1)
	Location     *time.Location // quiet hours time zone
	// Log gets short notes; JIDs are masked and message text is never logged.
	Log *log.Logger
	// NoMarkRead turns off mark_read_before_reply (read_receipts.allow_mark_read=false).
	NoMarkRead bool

	mu        sync.Mutex // guards chat state and the limit check with its reservation
	chats     map[string]*chatState
	nextSweep time.Time
}

func (s *Sender) agentAllowed(revision string) bool {
	return revision == "" || (s.AgentCurrent != nil && s.AgentCurrent(revision))
}

// Send runs the whole pipeline for one request. A request of several parts
// (a text and a file, a file and a sticker) that failed after some parts
// went out is resumed by the same request later: the parts already sent are
// skipped and their ids returned. On a partial failure the result holds the
// ids of the parts that were sent.
func (s *Sender) Send(ctx context.Context, req Request) (Result, error) {
	if !s.Cfg.Enabled {
		return Result{}, ErrDisabled
	}
	if !s.agentAllowed(req.AgentRevision) {
		return Result{}, ErrAgentPolicy
	}
	to, err := wa.ParseTarget(req.To)
	if err != nil {
		return Result{}, err
	}
	mentions, err := ParseMentions(req.Mentions)
	if err != nil {
		return Result{}, err
	}
	media, sticker, err := s.openFiles(&req)
	defer closeAll(media, sticker)
	if err != nil {
		return Result{}, err
	}
	var parts []part
	partCount := 1
	if req.Action == nil {
		parts, err = planAs(WithMentionTags(req.Text, mentions), media, req.AsVoice, req.AsDocument, sticker)
		if err != nil {
			return Result{}, err
		}
		partCount = len(parts)
	}
	target := to.String()
	hash, err := s.bodyHash(ctx, req)
	if err != nil {
		return Result{}, err
	}
	release, err := s.lockChat(ctx, target)
	if err != nil {
		return Result{}, err
	}
	defer release()

	if !s.agentAllowed(req.AgentRevision) {
		return Result{}, ErrAgentPolicy
	}
	now := s.now()
	if req.quietQueued {
		if quiet, until := s.InQuietHours(now); quiet {
			return Result{}, &DeferUntil{End: until}
		}
	}
	if req.Action != nil {
		if s.BuildAction == nil {
			return Result{}, errors.New("message action builder unavailable")
		}
		if _, err := s.actionAuthor(ctx, to, req.Action); err != nil {
			return Result{}, err
		}
	}
	resv, err := s.reserve(ctx, target, hash, now, partCount)
	if err != nil {
		return Result{}, err
	}
	defer resv.releaseUnused(context.WithoutCancel(ctx), s.DB)
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
	if err := s.beforeFirstPart(ctx, to, target, human, &waited); err != nil {
		return Result{}, err
	}
	if req.Action != nil {
		return s.sendAction(ctx, to, req.Action, resv, human, waited)
	}
	res, err := s.sendParts(ctx, to, target, req.ReplyTo, mentions, parts, resv, human, &waited, req.AgentRevision)
	res.Waited = waited
	if err == nil && len(res.IDs) < 2 {
		res.IDs = nil
	}
	return res, err
}

// openFiles opens and checks the files of a request once; the checked
// handles are the ones uploaded.
func (s *Sender) openFiles(req *Request) (media, sticker *os.File, err error) {
	if req.MediaPath != "" {
		if media, err = OpenMedia(req.MediaPath, s.Cfg.AllowedMediaDirs, s.Cfg.DeniedPaths, s.Cfg.ForeignMedia); err != nil {
			return nil, nil, err
		}
		req.MediaPath = media.Name()
	}
	if req.StickerPath != "" {
		if sticker, err = OpenMedia(req.StickerPath, s.Cfg.AllowedMediaDirs, s.Cfg.DeniedPaths, s.Cfg.ForeignMedia); err != nil {
			return media, nil, err
		}
		req.StickerPath = sticker.Name()
	}
	return media, sticker, nil
}

func closeAll(files ...*os.File) {
	for _, f := range files {
		if f != nil {
			f.Close()
		}
	}
}

// beforeFirstPart marks the chat read and waits like a person opening it.
func (s *Sender) beforeFirstPart(ctx context.Context, to types.JID, target string, human bool, waited *time.Duration) error {
	if s.Cfg.MarkReadBeforeReply && !s.NoMarkRead {
		s.markChatRead(ctx, to)
	}
	if !human {
		return nil
	}
	if err := s.pauseAfterLast(ctx, target, waited); err != nil {
		return err
	}
	return s.wait(ctx, s.between(0.4, 1.5), waited)
}

// sendParts sends the parts that the reservation has a slot for and skips
// the ones an earlier attempt sent.
func (s *Sender) sendParts(ctx context.Context, to types.JID, target string, replyTo *Reply, mentions []types.JID,
	parts []part, resv *reservation, human bool, waited *time.Duration, agentRevision string) (Result, error) {
	reply := s.fillReply(ctx, target, replyTo)
	splitText := len(parts) > 1 && parts[0].kind == store.KindText && parts[1].kind == store.KindText
	res := Result{Resumed: len(resv.done) > 0}
	first := true
	for i, p := range parts {
		if prev, ok := resv.done[i]; ok {
			if prev.MsgID != "" {
				res.add(prev.MsgID, time.Unix(prev.At, 0))
			}
			continue
		}
		if !first && human {
			if err := s.wait(ctx, s.between(s.Cfg.PauseBetween[0], s.Cfg.PauseBetween[1]), waited); err != nil {
				return res, err
			}
		}
		first = false
		if !s.agentAllowed(agentRevision) {
			return res, ErrAgentPolicy
		}
		partReply, partMentions := reply, mentions
		if splitText {
			if i > 0 {
				partReply = nil
			}
			partMentions = slices.DeleteFunc(slices.Clone(mentions), func(j types.JID) bool {
				return !strings.Contains(p.text, "@"+j.User)
			})
		}
		var pci *waE2E.ContextInfo
		if p.ctx {
			pci = BuildContext(partReply, partMentions)
		}
		msg, row, voice, err := s.build(ctx, p, pci)
		if err != nil {
			return res, err
		}
		if human {
			if err := s.present(ctx, to, p, voice, waited); err != nil {
				return res, err
			}
		}
		if !s.agentAllowed(agentRevision) {
			return res, ErrAgentPolicy
		}
		id, ts, err := s.sendOne(ctx, to, msg, resv, i)
		if id != "" {
			res.add(id, ts)
			s.record(context.WithoutCancel(ctx), target, id, ts, row, p.ctx, partReply, partMentions)
		}
		if err != nil {
			return res, err
		}
	}
	return res, nil
}

// sendOne hands one part to WhatsApp and records the outcome of its slot.
func (s *Sender) sendOne(ctx context.Context, to types.JID, msg *waE2E.Message, resv *reservation, part int) (string, time.Time, error) {
	if err := ctx.Err(); err != nil {
		return "", time.Time{}, err
	}
	sendCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), SendTimeout)
	id, ts, err := s.Transport.SendMessage(sendCtx, to, msg)
	cancel()
	keep := context.WithoutCancel(ctx)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
			// The message may have gone out: keep its slot as unknown, so a retry
			// skips this part instead of sending it twice.
			err = errors.Join(err, resv.mark(keep, s.DB, part, store.PartUnknown, ""))
			return "", time.Time{}, fmt.Errorf("send result unknown, the message may have been delivered: %w", err)
		}
		return "", time.Time{}, fmt.Errorf("send: %w", err)
	}
	s.noteSent(to.String())
	if ts.IsZero() {
		ts = s.now()
	}
	if err := resv.mark(keep, s.DB, part, store.PartSent, id); err != nil {
		return id, ts, fmt.Errorf("record sent part: %w", err)
	}
	return id, ts, nil
}

func (s *Sender) logf(format string, args ...any) {
	if s.Log != nil {
		s.Log.Printf(format, args...)
	}
}
