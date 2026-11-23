package sender

import (
	"context"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

// lockChat serialises sends to one chat so pauses between messages hold.
func (s *Sender) lockChat(target string) func() {
	s.mu.Lock()
	if s.chatLock == nil {
		s.chatLock = map[string]*sync.Mutex{}
		s.lastSent = map[string]time.Time{}
	}
	l, ok := s.chatLock[target]
	if !ok {
		l = &sync.Mutex{}
		s.chatLock[target] = l
	}
	s.mu.Unlock()
	l.Lock()
	return l.Unlock
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

// record stores our own message and remembers the send time for pacing.
// With an encrypted store the original file path is not kept, since it can
// reveal the attachment name.
func (s *Sender) record(ctx context.Context, target, id string, ts time.Time, row store.Message,
	withCtx bool, reply *Reply, mentions []types.JID) {
	s.mu.Lock()
	s.lastSent[target] = s.now()
	s.mu.Unlock()

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
