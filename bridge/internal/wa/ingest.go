package wa

import (
	"context"
	"errors"
	"log"
	"time"

	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// Ingest turns WhatsApp events into database rows and webhook events.
// It has no network dependencies, so tests can drive it with synthetic events.
type Ingest struct {
	DB *store.DB
	// Title returns a name for a chat that has none yet without waiting for
	// the network (group names are looked up in the background); push is
	// the sender's own display name for incoming direct messages. May be nil.
	Title func(ctx context.Context, chat types.JID, push string) string
	// Self returns our phone-number and LID addresses.
	Self func() (pn, lid types.JID)
	// AltOf returns the other address (LID for a phone number, or the
	// reverse) from the session's LID map; empty when unknown. May be nil.
	AltOf func(ctx context.Context, j types.JID) types.JID
	// GroupAdmin reports whether user is an admin of group; known is false
	// while the member list is not cached yet (a lookup is then started). May be nil.
	GroupAdmin func(group, user types.JID) (admin, known bool)
	// Emit hands an event to the webhook dispatcher. May be nil.
	Emit func(event string, data any)
	// AutoRead marks a live incoming message read. May be nil.
	AutoRead    func(ctx context.Context, m *store.Message)
	RecordCalls bool
	// Log gets short notes; JIDs are masked and message text is never logged.
	Log *log.Logger
}

func (in *Ingest) emit(event string, data any) {
	if in.Emit != nil {
		in.Emit(event, data)
	}
}

func (in *Ingest) logf(format string, args ...any) {
	if in.Log != nil {
		in.Log.Printf(format, args...)
	}
}

func (in *Ingest) self() (types.JID, types.JID) {
	if in.Self == nil {
		return types.JID{}, types.JID{}
	}
	return in.Self()
}

func (in *Ingest) isSelf(j types.JID) bool {
	if j.User == "" {
		return false
	}
	pn, lid := in.self()
	return j.User == pn.User || j.User == lid.User
}

// sameUser reports whether any of the actor addresses is the stored author,
// asking the LID map when the addresses differ in kind.
func (in *Ingest) sameUser(ctx context.Context, author string, actors ...types.JID) bool {
	a, err := types.ParseJID(author)
	if err != nil || a.User == "" {
		return false
	}
	for _, j := range actors {
		if j.User != "" && j.User == a.User {
			return true
		}
	}
	if in.AltOf == nil {
		return false
	}
	alt := in.AltOf(ctx, a.ToNonAD())
	for _, j := range actors {
		if j.User == "" {
			continue
		}
		if alt.User != "" && j.User == alt.User {
			return true
		}
		if other := in.AltOf(ctx, j.ToNonAD()); other.User != "" && other.User == a.User {
			return true
		}
	}
	return false
}

// mayChange reports whether actor may edit or delete m: the original author,
// or we ourselves for our own messages.
func (in *Ingest) mayChange(ctx context.Context, m *store.Message, actor, actorAlt types.JID) bool {
	if m.Outgoing {
		return in.isSelf(actor) || in.isSelf(actorAlt)
	}
	return in.sameUser(ctx, m.Author, actor, actorAlt)
}

func unixOr(t time.Time) int64 {
	if t.IsZero() {
		return time.Now().Unix()
	}
	return t.Unix()
}

// Message stores one message. Live messages also update chat titles, reach
// webhooks and may be marked read; history messages do not. A message whose
// id is already stored for another author (or the other direction) is not
// written: ids are chosen by the sender, so a reused id must not replace
// someone else's text.
func (in *Ingest) Message(ctx context.Context, evt *events.Message, live bool) (*store.Message, error) {
	if evt == nil || evt.Message == nil {
		return nil, nil
	}
	if live {
		ctx = store.WithOrigin(ctx, store.OriginLive)
	} else {
		ctx = store.WithOrigin(ctx, store.OriginHistory)
	}
	chat := evt.Info.Chat.ToNonAD()
	if chat.Server == types.BroadcastServer || chat.User == "" {
		return nil, nil
	}
	payload := unwrap(evt.Message)
	actor, actorAlt := evt.Info.Sender.ToNonAD(), evt.Info.SenderAlt.ToNonAD()
	if pm := payload.GetProtocolMessage(); pm != nil {
		target := pm.GetKey().GetID()
		switch {
		case target == "":
		case pm.GetType() == waE2E.ProtocolMessage_MESSAGE_EDIT:
			if text := Describe(pm.GetEditedMessage()).Text; text != "" {
				return nil, in.change(ctx, store.PendingChange{Conv: chat.String(), ID: target, Kind: store.ChangeEdit,
					Actor: actor.String(), ActorAlt: actorAlt.String(), Body: text, At: unixOr(evt.Info.Timestamp)})
			}
		case pm.GetType() == waE2E.ProtocolMessage_REVOKE:
			return nil, in.change(ctx, store.PendingChange{Conv: chat.String(), ID: target, Kind: store.ChangeRevoke,
				Actor: actor.String(), ActorAlt: actorAlt.String(), At: unixOr(evt.Info.Timestamp)})
		}
		return nil, nil
	}
	if r := payload.GetReactionMessage(); r != nil {
		if target := r.GetKey().GetID(); target != "" {
			at := unixOr(evt.Info.Timestamp)
			if ms := r.GetSenderTimestampMS(); ms > 0 {
				at = ms / 1000
			}
			return nil, in.DB.SetReaction(ctx, store.Reaction{Conv: chat.String(), MsgID: target,
				Reactor: actor.String(), Emoji: r.GetText(), At: at})
		}
		return nil, nil
	}
	c := Describe(payload)
	if c.Kind == "" || (c.Kind == store.KindText && c.Text == "") {
		return nil, nil
	}
	reply := ReplyOf(c.Context)
	m := &store.Message{
		Conv:           chat.String(),
		ID:             evt.Info.ID,
		Author:         actor.String(),
		Outgoing:       evt.Info.IsFromMe,
		SentAt:         unixOr(evt.Info.Timestamp),
		Body:           c.Text,
		Kind:           c.Kind,
		AttachmentName: c.FileName,
		Mime:           c.Mime,
		ByteSize:       int64(c.Size),
		DirectPath:     c.DirectPath,
		CDNURL:         c.URL,
		MediaKey:       c.MediaKey,
		PlainSHA256:    c.PlainHash,
		EncSHA256:      c.EncHash,
		ReplyToID:      reply.ID,
		ReplyToAuthor:  reply.Author,
		ReplyPreview:   reply.Preview,
		Mentioned:      c.Context.GetMentionedJID(),
	}
	if m.Author == "" && m.Outgoing {
		pn, _ := in.self()
		m.Author = pn.String()
	}
	if prev, err := in.DB.GetMessage(ctx, m.Conv, m.ID); err == nil {
		same := prev.Outgoing == m.Outgoing && (prev.Outgoing || prev.Author == "" || in.sameUser(ctx, prev.Author, actor, actorAlt))
		if !same {
			in.logf("ignored message %s in %s: the id belongs to another author (%s, now %s)",
				m.ID, common.MaskJID(m.Conv), common.MaskJID(prev.Author), common.MaskJID(m.Author))
			return nil, nil
		}
	} else if !errors.Is(err, store.ErrNotFound) {
		return nil, err
	}
	if err := in.DB.PutMessage(ctx, m); err != nil {
		return nil, err
	}
	if err := in.applyPending(ctx, m); err != nil {
		in.logf("pending changes for %s: %v", m.ID, err)
	}

	title := ""
	if live && in.Title != nil {
		if conv, err := in.DB.GetConversation(ctx, m.Conv); err != nil || conv.Title == "" {
			push := ""
			if !m.Outgoing && chat.Server != types.GroupServer {
				push = evt.Info.PushName
			}
			title = in.Title(ctx, chat, push)
		}
	}
	if err := in.DB.TouchConversation(ctx, store.Conversation{
		JID: m.Conv, Title: title, IsGroup: chat.Server == types.GroupServer, LastActivity: m.SentAt,
	}); err != nil {
		return m, err
	}

	if live {
		in.emit("message", m)
		if !m.Outgoing && in.AutoRead != nil {
			in.AutoRead(ctx, m)
		}
	}
	return m, nil
}

// change applies an edit or deletion now, or keeps it until the message arrives.
func (in *Ingest) change(ctx context.Context, p store.PendingChange) error {
	m, err := in.DB.GetMessage(ctx, p.Conv, p.ID)
	if errors.Is(err, store.ErrNotFound) {
		return in.DB.AddPending(ctx, p)
	}
	if err != nil {
		return err
	}
	return in.apply(ctx, m, p)
}

func (in *Ingest) apply(ctx context.Context, m *store.Message, p store.PendingChange) error {
	actor, _ := types.ParseJID(p.Actor)
	alt, _ := types.ParseJID(p.ActorAlt)
	author := in.mayChange(ctx, m, actor, alt)
	switch p.Kind {
	case store.ChangeEdit:
		if !author {
			in.logf("ignored an edit of %s by %s (not the author)", m.ID, common.MaskJID(p.Actor))
			return nil
		}
		_, err := in.DB.EditBody(ctx, m.Conv, m.ID, p.Body, p.At)
		return err
	case store.ChangeRevoke:
		if author {
			_, err := in.DB.MarkDeleted(ctx, m.Conv, m.ID, p.Actor, p.At)
			return err
		}
		group, _ := types.ParseJID(m.Conv)
		if group.Server != types.GroupServer {
			in.logf("ignored a deletion of %s by %s (not the author)", m.ID, common.MaskJID(p.Actor))
			return nil
		}
		// Only group admins may delete other members' messages. Without a
		// confirmed admin the deletion is kept as unverified and the text stays.
		if in.GroupAdmin != nil {
			for _, j := range []types.JID{actor, alt} {
				if j.User == "" {
					continue
				}
				if admin, known := in.GroupAdmin(group, j.ToNonAD()); known && admin {
					_, err := in.DB.MarkDeleted(ctx, m.Conv, m.ID, p.Actor, p.At)
					return err
				}
			}
		}
		_, err := in.DB.MarkDeletedUnverified(ctx, m.Conv, m.ID, p.Actor, p.At)
		return err
	}
	return nil
}

func (in *Ingest) applyPending(ctx context.Context, m *store.Message) error {
	pending, err := in.DB.TakePending(ctx, m.Conv, m.ID)
	if err != nil {
		return err
	}
	for _, p := range pending {
		if err := in.apply(ctx, m, p); err != nil {
			return err
		}
	}
	return nil
}

// GroupAdmins confirms unverified deletions in a group once its admins are known.
func (in *Ingest) GroupAdmins(ctx context.Context, group types.JID, admins []types.JID) {
	list := make([]string, 0, len(admins))
	for _, a := range admins {
		if a.User != "" {
			list = append(list, a.ToNonAD().String())
		}
	}
	if n, err := in.DB.VerifyDeletions(ctx, group.ToNonAD().String(), list); err != nil {
		in.logf("verify deletions in %s: %v", common.MaskJID(group.String()), err)
	} else if n > 0 {
		in.logf("confirmed %d deletions by admins in %s", n, common.MaskJID(group.String()))
	}
}

// WebParser turns a history-sync entry into a message event.
type WebParser func(chat types.JID, web *waWeb.WebMessageInfo) (*events.Message, error)

// History stores a history-sync blob and returns the number of stored messages.
func (in *Ingest) History(ctx context.Context, data *waHistorySync.HistorySync, parse WebParser) int {
	stored := 0
	for _, conv := range data.GetConversations() {
		chat, err := types.ParseJID(conv.GetID())
		if err != nil || chat.User == "" {
			continue
		}
		chat = chat.ToNonAD()
		var latest int64
		for _, item := range conv.GetMessages() {
			evt, err := parse(chat, item.GetMessage())
			if err != nil || evt == nil {
				continue
			}
			m, err := in.Message(ctx, evt, false)
			if err != nil {
				in.logf("history: store %s in %s: %v", evt.Info.ID, common.MaskJID(chat.String()), err)
				continue
			}
			if m != nil {
				stored++
				if m.SentAt > latest {
					latest = m.SentAt
				}
			}
		}
		title := conv.GetName()
		if title == "" {
			title = conv.GetDisplayName()
		}
		if last := int64(conv.GetLastMsgTimestamp()); last > latest && latest > 0 {
			latest = last
		}
		if title != "" || latest > 0 {
			_ = in.DB.TouchConversation(ctx, store.Conversation{
				JID: chat.String(), Title: title, IsGroup: chat.Server == types.GroupServer, LastActivity: latest,
			})
		}
	}
	return stored
}

// GroupName renames a group chat (group info changes, joins, background lookups).
func (in *Ingest) GroupName(ctx context.Context, group types.JID, name string, force bool) {
	if _, err := in.DB.SetTitle(ctx, group.ToNonAD().String(), name, force); err != nil {
		in.logf("title for %s: %v", common.MaskJID(group.String()), err)
	}
}

// PersonName updates direct chats of one person (several addresses). An
// address book name replaces the title; a push name only fills an empty one.
func (in *Ingest) PersonName(ctx context.Context, name string, fromAddressBook bool, jids ...types.JID) {
	for _, j := range jids {
		if j.IsEmpty() || j.User == "" {
			continue
		}
		if _, err := in.DB.SetTitle(ctx, j.ToNonAD().String(), name, fromAddressBook); err != nil {
			in.logf("title for %s: %v", common.MaskJID(j.String()), err)
		}
	}
}

func receiptName(t types.ReceiptType) string {
	if t == types.ReceiptTypeDelivered {
		return "delivered"
	}
	return string(t)
}

// Receipt records our own read receipts (read on another device) and forwards all receipts.
func (in *Ingest) Receipt(ctx context.Context, evt *events.Receipt) {
	chat := evt.Chat.ToNonAD().String()
	at := unixOr(evt.Timestamp)
	if evt.IsFromMe && (evt.Type == types.ReceiptTypeRead || evt.Type == types.ReceiptTypeReadSelf) {
		if err := in.DB.MarkRead(ctx, chat, evt.MessageIDs, at); err != nil {
			in.logf("receipt: %v", err)
		}
	}
	in.emit("receipt", map[string]any{
		"chat":    chat,
		"ids":     evt.MessageIDs,
		"type":    receiptName(evt.Type),
		"from":    evt.Sender.ToNonAD().String(),
		"from_me": evt.IsFromMe,
		"at":      at,
	})
}

func hasChild(n *waBinary.Node, tag string) bool {
	if n == nil {
		return false
	}
	for _, c := range n.GetChildren() {
		if c.Tag == tag {
			return true
		}
	}
	return false
}

// callParty works out the other side of a call and whether we started it.
func (in *Ingest) callParty(meta types.BasicCallMeta) (string, bool) {
	creator := meta.CallCreator.ToNonAD()
	if !in.isSelf(creator) {
		if creator.Server == types.HiddenUserServer && !meta.CallCreatorAlt.IsEmpty() {
			creator = meta.CallCreatorAlt.ToNonAD()
		}
		return creator.String(), false
	}
	from := meta.From.ToNonAD()
	if !from.IsEmpty() && !in.isSelf(from) && from.Server != types.GroupServer {
		return from.String(), true
	}
	return creator.String(), true
}

func (in *Ingest) noteCall(ctx context.Context, meta types.BasicCallMeta, video, group bool) error {
	if meta.CallID == "" {
		return nil
	}
	peer, outgoing := in.callParty(meta)
	g := ""
	switch {
	case !meta.GroupJID.IsEmpty():
		g = meta.GroupJID.ToNonAD().String()
	case group && meta.From.Server == types.GroupServer:
		g = meta.From.ToNonAD().String()
	}
	return in.DB.NoteCall(ctx, store.Call{
		ID: meta.CallID, Peer: peer, Group: g, Outgoing: outgoing, Video: video, StartedAt: unixOr(meta.Timestamp),
	})
}

// Call records call signalling events in call_log.
func (in *Ingest) Call(ctx context.Context, evt any) {
	if !in.RecordCalls {
		return
	}
	var meta types.BasicCallMeta
	var err error
	switch e := evt.(type) {
	case *events.CallOffer:
		meta = e.BasicCallMeta
		err = in.noteCall(ctx, meta, hasChild(e.Data, "video"), false)
	case *events.CallOfferNotice:
		meta = e.BasicCallMeta
		err = in.noteCall(ctx, meta, e.Media == "video", e.Type == "group")
	case *events.CallAccept:
		meta = e.BasicCallMeta
		if err = in.noteCall(ctx, meta, false, false); err == nil {
			err = in.DB.SetCallOutcome(ctx, meta.CallID, store.CallAnswered)
		}
	case *events.CallReject:
		meta = e.BasicCallMeta
		if err = in.noteCall(ctx, meta, false, false); err == nil {
			err = in.DB.SetCallOutcome(ctx, meta.CallID, store.CallRejected)
		}
	case *events.CallTerminate:
		meta = e.BasicCallMeta
		if err = in.noteCall(ctx, meta, false, false); err == nil {
			err = in.DB.EndCall(ctx, meta.CallID, unixOr(meta.Timestamp))
		}
	default:
		return
	}
	if meta.CallID == "" {
		return
	}
	if err != nil {
		in.logf("call %s: %v", meta.CallID, err)
		return
	}
	if c, err := in.DB.GetCall(ctx, meta.CallID); err == nil {
		in.emit("call", c)
	}
}
