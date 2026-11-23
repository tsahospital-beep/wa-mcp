package sender

import (
	"context"
	"errors"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// ErrMediaAndSticker is returned when one stored entry would need two files.
var ErrMediaAndSticker = errors.New("a scheduled message can carry media or a sticker, not both; send them separately")

// DeferUntil keeps a quiet-hours job pending under its original schedule ID.
type DeferUntil struct{ End time.Time }

func (e *DeferUntil) Error() string { return "queued send is still within quiet hours" }

// KindOf names the stored kind of a request: text, sticker, voice or the
// media kind of media_path (image, video, audio, document).
func KindOf(mediaPath string, asVoice bool, stickerPath string) (kind, path string, err error) {
	switch {
	case stickerPath != "" && mediaPath != "":
		return "", "", ErrMediaAndSticker
	case stickerPath != "":
		kind, err = mediaKind(stickerPath, false, true)
		return kind, stickerPath, err
	case mediaPath != "":
		kind, err = mediaKind(mediaPath, asVoice, false)
		return kind, mediaPath, err
	}
	return store.KindText, "", nil
}

// queue stores the request as a one-time schedule at the end of quiet hours,
// keeping its kind and pacing flag so the later send matches this one.
func (s *Sender) queue(ctx context.Context, req Request, target string, at, now time.Time) (Result, error) {
	kind, media, err := KindOf(req.MediaPath, req.AsVoice, req.StickerPath)
	if err != nil {
		return Result{}, err
	}
	if req.AsDocument && !req.AsVoice && req.MediaPath != "" {
		kind = store.KindDocument
	}
	reply := ""
	if req.ReplyTo != nil {
		reply = req.ReplyTo.ID
	}
	id, err := s.DB.AddScheduled(ctx, &store.Scheduled{
		QuietQueued:   true,
		AgentRevision: req.AgentRevision, Target: target, Body: req.Text, MediaPath: media, Kind: kind, Human: req.Human, ReplyToID: reply,
		Mentions: req.Mentions, NextRun: at.Unix(), Recurrence: "once", CreatedAt: now.Unix(),
	})
	if err != nil {
		return Result{}, err
	}
	s.logf("quiet hours: message to %s queued until %s (schedule %d)", common.MaskJID(target), at.Format(time.RFC3339), id)
	return Result{Queued: true, ScheduledID: id}, nil
}

// RequestFromSchedule rebuilds a send request from a stored schedule. Entries
// without a stored kind (older rows) use the extension: .ogg/.opus become
// voice notes and .webp a sticker.
func RequestFromSchedule(sc *store.Scheduled) Request {
	req := Request{AgentRevision: sc.AgentRevision, To: sc.Target, Text: sc.Body, Mentions: sc.Mentions, Human: sc.Human, Scheduled: true}
	req.quietQueued = sc.QuietQueued
	if sc.ReplyToID != "" {
		req.ReplyTo = &Reply{ID: sc.ReplyToID}
	}
	p := sc.MediaPath
	switch sc.Kind {
	case store.KindSticker:
		req.StickerPath = p
	case store.KindVoice:
		req.MediaPath, req.AsVoice = p, true
	case store.KindText:
	case store.KindDocument:
		req.MediaPath, req.AsDocument = p, true
	case store.KindImage, store.KindVideo, store.KindAudio:
		req.MediaPath = p
	default:
		switch {
		case p == "":
		case isStickerFile(p):
			req.StickerPath = p
		case isOpusFile(p):
			req.MediaPath, req.AsVoice = p, true
		default:
			req.MediaPath = p
		}
	}
	return req
}
