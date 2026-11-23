package sender

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

var ErrAction = errors.New("invalid message action")
var ErrNotOwn = errors.New("only your own messages can be edited or deleted")
var ErrSourceChanged = errors.New("the source text changed; review it again")

// Resolve the participant from the account store, never from the caller.
func (s *Sender) actionAuthor(ctx context.Context, to types.JID, a *wa.MessageAction) (types.JID, error) {
	if a.MessageID == "" || strings.HasPrefix(a.MessageID, store.ImportedPrefix) {
		return types.EmptyJID, fmt.Errorf("%w: use a WhatsApp message id", ErrAction)
	}
	if a.Kind != "forward" && a.Chat != to.String() {
		return types.EmptyJID, fmt.Errorf("%w: target must match source chat", ErrAction)
	}
	m, err := s.DB.GetMessage(ctx, a.Chat, a.MessageID)
	if err != nil {
		return types.EmptyJID, err
	}
	if m.DeletedAt != 0 {
		return types.EmptyJID, fmt.Errorf("%w: message was deleted", ErrAction)
	}
	if (a.Kind == "edit" || a.Kind == "delete") && !m.Outgoing {
		return types.EmptyJID, ErrNotOwn
	}
	switch a.Kind {
	case "forward", "edit":
		if m.Kind != store.KindText || strings.TrimSpace(a.Value) == "" || utf8.RuneCountInString(a.Value) > messageChars {
			return types.EmptyJID, fmt.Errorf("%w: use a text message with 1 to %d characters", ErrAction, messageChars)
		}
		if a.Kind == "edit" && s.now().Sub(time.Unix(m.SentAt, 0)) > whatsmeow.EditWindow {
			return types.EmptyJID, fmt.Errorf("%w: edit window has expired", ErrAction)
		}
		if a.Kind == "forward" && a.Value != m.Body {
			return types.EmptyJID, ErrSourceChanged
		}
	case "react":
		if len(a.Value) > store.MaxReactionText || strings.IndexFunc(a.Value, func(r rune) bool { return unicode.IsControl(r) || unicode.IsSpace(r) }) >= 0 {
			return types.EmptyJID, fmt.Errorf("%w: give a reaction or an empty string to remove it", ErrAction)
		}
		if !m.Outgoing {
			return wa.ParseChat(m.Author)
		}
	case "delete":
	default:
		return types.EmptyJID, ErrAction
	}
	return types.EmptyJID, nil
}

func (s *Sender) sendAction(ctx context.Context, to types.JID, a *wa.MessageAction, resv *reservation, human bool, waited time.Duration) (Result, error) {
	if human && a.Kind == "edit" {
		if err := s.present(ctx, to, part{text: a.Value}, VoiceInfo{}, &waited); err != nil {
			return Result{}, err
		}
	}
	// Recheck after pacing, including when this is a quiet-hours replay.
	author, err := s.actionAuthor(ctx, to, a)
	if err != nil {
		return Result{}, err
	}
	msg, err := s.BuildAction(*a, to, author)
	if err != nil {
		return Result{}, err
	}
	id, ts, err := s.sendOne(ctx, to, msg, resv, 0)
	res := Result{ID: id, SentAt: ts, Waited: waited}
	if id == "" {
		return res, err
	}
	keep := context.WithoutCancel(ctx)
	target := to.String()
	own := s.Transport.OwnJID().String()
	var saveErr error
	switch a.Kind {
	case "edit":
		_, saveErr = s.DB.EditBody(keep, target, a.MessageID, a.Value, ts.Unix())
	case "delete":
		_, saveErr = s.DB.MarkDeleted(keep, target, a.MessageID, own, ts.Unix())
	case "react":
		saveErr = s.DB.SetReaction(keep, store.Reaction{Conv: target, MsgID: a.MessageID, Reactor: own, Emoji: a.Value, At: ts.Unix()})
	case "forward":
		s.record(keep, target, id, ts, store.Message{Kind: store.KindText, Body: a.Value}, false, nil, nil)
	}
	if err != nil || saveErr != nil {
		res.IDs = []string{id}
	}
	return res, errors.Join(err, saveErr)
}
