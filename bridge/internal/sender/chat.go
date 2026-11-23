package sender

import (
	"context"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

type chatState struct {
	gate     chan struct{}
	users    int
	lastSent time.Time
}

// lockChat serialises sends to one chat so pauses between messages hold.
func (s *Sender) lockChat(ctx context.Context, target string) (func(), error) {
	s.mu.Lock()
	if s.chats == nil {
		s.chats = make(map[string]*chatState)
	}
	now := s.now()
	if !now.Before(s.nextSweep) {
		var maxGap time.Duration
		if len(s.Cfg.PauseBetween) == 2 {
			maxGap = seconds(s.Cfg.PauseBetween[1])
		}
		for jid, chat := range s.chats {
			if chat.users == 0 && now.Sub(chat.lastSent) >= maxGap {
				delete(s.chats, jid)
			}
		}
		s.nextSweep = now.Add(time.Minute)
	}
	chat := s.chats[target]
	if chat == nil {
		chat = &chatState{gate: make(chan struct{}, 1)}
		s.chats[target] = chat
	}
	chat.users++
	s.mu.Unlock()
	done := func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		chat.users--
		if chat.users == 0 && chat.lastSent.IsZero() {
			delete(s.chats, target)
		}
	}
	select {
	case chat.gate <- struct{}{}:
		release := func() {
			<-chat.gate
			done()
		}
		if err := ctx.Err(); err != nil {
			release()
			return nil, err
		}
		return release, nil
	case <-ctx.Done():
		done()
		return nil, ctx.Err()
	}
}

// fillReply completes author and preview of a reply from the database.
func (s *Sender) fillReply(ctx context.Context, conv string, r *Reply) *Reply {
	if r == nil || r.ID == "" {
		return nil
	}
	out := *r
	if out.Author != "" {
		if j, err := wa.ParseTarget(out.Author); err == nil {
			out.Author = j.String()
		}
	}
	if out.Author != "" && out.Preview != "" {
		return &out
	}
	m, err := s.DB.GetMessage(ctx, conv, out.ID)
	if err != nil {
		return &out
	}
	if out.Author == "" {
		out.Author = m.Author
		if own := s.Transport.OwnJID(); m.Outgoing && !own.IsEmpty() {
			out.Author = own.String()
		}
	}
	if out.Preview == "" {
		out.Preview = wa.Shorten(m.Placeholder(), 200)
	}
	return &out
}

// markChatRead marks the latest unread incoming messages of the chat read.
func (s *Sender) markChatRead(ctx context.Context, chat types.JID) {
	unread, err := s.DB.UnreadIncoming(ctx, chat.String(), 20)
	if err != nil || len(unread) == 0 {
		return
	}
	byAuthor := map[string][]string{}
	for _, m := range unread {
		byAuthor[m.Author] = append(byAuthor[m.Author], m.ID)
	}
	now := s.now()
	for author, ids := range byAuthor {
		var sender types.JID
		if chat.Server == types.GroupServer {
			j, err := wa.ParseTarget(author)
			if err != nil {
				continue
			}
			sender = j
		}
		if err := s.Transport.MarkRead(ctx, ids, now, chat, sender); err != nil {
			s.logf("mark read in %s: %v", common.MaskJID(chat.String()), err)
			continue
		}
		_ = s.DB.MarkRead(ctx, chat.String(), ids, now.Unix())
	}
}

func (s *Sender) noteSent(target string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.chats[target].lastSent = s.now()
}

// record stores our own message.
// With an encrypted store the original file path is not kept, since it can
// reveal the attachment name.
func (s *Sender) record(ctx context.Context, target, id string, ts time.Time, row store.Message,
	withCtx bool, reply *Reply, mentions []types.JID) {
	ctx = store.WithOrigin(ctx, store.OriginSent)
	row.Conv, row.ID, row.Outgoing, row.SentAt = target, id, true, ts.Unix()
	row.Author = s.Transport.OwnJID().String()
	if s.DB.Encrypted() {
		row.LocalPath = ""
	}
	if withCtx {
		if reply != nil {
			row.ReplyToID, row.ReplyToAuthor, row.ReplyPreview = reply.ID, reply.Author, reply.Preview
		}
		for _, j := range mentions {
			row.Mentioned = append(row.Mentioned, j.String())
		}
	}
	if err := s.DB.PutMessage(ctx, &row); err != nil {
		s.logf("store sent message %s: %v", id, err)
	}
	to, _ := wa.ParseTarget(target)
	_ = s.DB.TouchConversation(ctx, store.Conversation{JID: target, IsGroup: to.Server == types.GroupServer, LastActivity: row.SentAt})
}
