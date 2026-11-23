package wabridge

import (
	"context"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

// Deps is what route handlers may use. It is safe for concurrent use.
type Deps interface {
	// Paths returns the resolved folders and files of this bridge.
	Paths() Paths
	// OwnJID is our phone-number JID ("" before the first login).
	OwnJID() string
	// Connected reports whether the bridge is connected to WhatsApp.
	Connected() bool
	// Send runs the sending pipeline: sending.enabled, limits, duplicate
	// check, quiet hours and human-like pacing. The values of ctx reach
	// OnMessageStored for the messages of this send.
	Send(ctx context.Context, req SendRequest) (SendResult, error)
	// DownloadMedia saves the attachment of a stored message. With target
	// "" it goes to downloads_dir as for /api/v1/media/download; otherwise
	// to the file target, unless a file of the right size is already there.
	// An expired copy is requested from the phone again.
	DownloadMedia(ctx context.Context, chat, id, target string) (Media, error)
	// RequestHistory asks our phone for up to count (1 to 100) messages
	// older than the message beforeID, or than the oldest stored message of
	// the chat when beforeID is "". ErrNotFound means there is no anchor.
	RequestHistory(ctx context.Context, chat string, count int, beforeID string) (HistoryAnchor, error)
	// SaveProfilePicture saves the profile picture of jid as
	// dir/avatars/<user>.jpg (dir "" means downloads_dir) and returns the path.
	SaveProfilePicture(ctx context.Context, jid, dir string) (string, error)
	// Store reads the bridge database.
	Store() Reader
}

// Reader is the read side of the bridge database.
type Reader interface {
	// Message returns one message (ErrNotFound when missing). The text of a
	// deleted message is empty.
	Message(ctx context.Context, chat, id string) (StoredMessage, error)
	// Messages lists messages, newest first unless q.Ascending.
	Messages(ctx context.Context, q MessageQuery) ([]StoredMessage, error)
	// Chat returns one chat (ErrNotFound when missing).
	Chat(ctx context.Context, jid string) (StoredChat, error)
	// Chats lists chats by recent activity, optionally filtered by title or JID.
	Chats(ctx context.Context, query string, limit, offset int) ([]StoredChat, error)
}

// MessageQuery filters Reader.Messages. Zero values mean no filter.
type MessageQuery struct {
	Chat           string
	After, Before  time.Time
	Query          string // case-insensitive text search
	Limit, Offset  int    // Limit 0 means 100
	Ascending      bool
	IncludeDeleted bool
}

// Paths are the resolved locations of one bridge.
type Paths struct {
	ConfigFile   string
	StoreDir     string
	DownloadsDir string
	ExportsDir   string
	SessionDB    string
	MessagesDB   string
}

// SendRequest is one send. Text, MediaPath and StickerPath may be combined;
// files must be inside the folders allowed by the config.
type SendRequest struct {
	To          string // JID or phone number with country code
	Text        string
	MediaPath   string
	AsVoice     bool // send an Ogg Opus MediaPath as a voice note
	AsDocument  bool // send MediaPath as a document whatever its type
	StickerPath string
	ReplyToID   string
	Mentions    []string
	// Human turns human-like pacing off (false) or on (true) for this send;
	// nil follows sending.human_like.
	Human *bool
}

// SendResult describes a finished or queued send.
type SendResult struct {
	ID          string   // first message id
	IDs         []string // all ids when the send was split into several messages
	SentAt      time.Time
	Waited      time.Duration // time spent in human-like pauses
	Queued      bool          // held for the end of quiet hours
	ScheduledID int64
}

// Media is a saved attachment.
type Media struct {
	Path           string
	Kind           string
	AttachmentName string
}

// HistoryAnchor is the message a history request was anchored on.
type HistoryAnchor struct {
	ID       string
	SentAt   time.Time
	Outgoing bool
}

type deps struct {
	cfg    *config.Config
	db     *store.DB
	client *wa.Client
	send   func(ctx context.Context, req sender.Request) (sender.Result, error)
	fetch  *media.Fetcher
}

func (d *deps) Paths() Paths {
	return Paths{ConfigFile: d.cfg.File, StoreDir: d.cfg.StoreDir, DownloadsDir: d.cfg.DownloadsDir,
		ExportsDir: d.cfg.ExportsDir, SessionDB: d.cfg.SessionDB(), MessagesDB: d.cfg.MessagesDB()}
}

func (d *deps) OwnJID() string {
	j := d.client.OwnJID()
	if j.IsEmpty() {
		return ""
	}
	return j.String()
}

func (d *deps) Connected() bool { return d.client.Connected() }

func (d *deps) Send(ctx context.Context, req SendRequest) (SendResult, error) {
	if !d.cfg.Sending.Enabled {
		return SendResult{}, ErrSendingDisabled
	}
	if !d.client.LoggedIn() {
		return SendResult{}, ErrNotLoggedIn
	}
	if !d.client.Connected() {
		return SendResult{}, ErrNotConnected
	}
	in := sender.Request{To: req.To, Text: req.Text, MediaPath: req.MediaPath, AsVoice: req.AsVoice,
		AsDocument: req.AsDocument, StickerPath: req.StickerPath, Mentions: req.Mentions, Human: req.Human}
	if req.ReplyToID != "" {
		in.ReplyTo = &sender.Reply{ID: req.ReplyToID}
	}
	res, err := d.send(ctx, in)
	return SendResult{ID: res.ID, IDs: res.IDs, SentAt: res.SentAt, Waited: res.Waited,
		Queued: res.Queued, ScheduledID: res.ScheduledID}, err
}

func (d *deps) DownloadMedia(ctx context.Context, chat, id, target string) (Media, error) {
	j, err := wa.ParseChat(chat)
	if err != nil {
		return Media{}, err
	}
	var res *media.Result
	if target == "" {
		res, err = d.fetch.Fetch(ctx, j.String(), id)
	} else {
		res, err = d.fetch.FetchTo(ctx, j.String(), id, target)
	}
	if err != nil {
		return Media{}, err
	}
	return Media{Path: res.Path, Kind: res.Kind, AttachmentName: res.AttachmentName}, nil
}

func (d *deps) RequestHistory(ctx context.Context, chat string, count int, beforeID string) (HistoryAnchor, error) {
	j, err := wa.ParseChat(chat)
	if err != nil {
		return HistoryAnchor{}, err
	}
	if count <= 0 {
		count = 50
	}
	count = min(count, 100)
	var anchor *store.Message
	if beforeID != "" {
		anchor, err = d.db.GetMessage(ctx, j.String(), beforeID)
	} else {
		anchor, err = d.db.OldestMessage(ctx, j.String())
	}
	if err != nil {
		return HistoryAnchor{}, err
	}
	out := HistoryAnchor{ID: anchor.ID, SentAt: unixTime(anchor.SentAt), Outgoing: anchor.Outgoing}
	return out, d.client.RequestHistory(ctx, anchor, count)
}

func (d *deps) SaveProfilePicture(ctx context.Context, jid, dir string) (string, error) {
	j, err := wa.ParseChat(jid)
	if err != nil {
		return "", err
	}
	if dir == "" {
		dir = d.cfg.DownloadsDir
	}
	return media.SaveAvatar(ctx, d.client, nil, dir, j)
}

func (d *deps) Store() Reader { return reader{d.db} }

type reader struct{ db *store.DB }

func (r reader) title(ctx context.Context, conv string) string {
	if c, err := r.db.GetConversation(ctx, conv); err == nil {
		return c.Title
	}
	return ""
}

func (r reader) Message(ctx context.Context, chat, id string) (StoredMessage, error) {
	j, err := wa.ParseChat(chat)
	if err != nil {
		return StoredMessage{}, err
	}
	m, err := r.db.GetMessage(ctx, j.String(), id)
	if err != nil {
		return StoredMessage{}, err
	}
	return toStored("", m, r.title(ctx, m.Conv)), nil
}

func (r reader) Messages(ctx context.Context, q MessageQuery) ([]StoredMessage, error) {
	conv := ""
	if q.Chat != "" {
		j, err := wa.ParseChat(q.Chat)
		if err != nil {
			return nil, err
		}
		conv = j.String()
	}
	list, err := r.db.ListMessages(ctx, store.MessageQuery{Conv: conv, After: unixOf(q.After), Before: unixOf(q.Before),
		Query: q.Query, Limit: q.Limit, Offset: q.Offset, Ascending: q.Ascending, IncludeDeleted: q.IncludeDeleted})
	if err != nil {
		return nil, err
	}
	titles := map[string]string{}
	out := make([]StoredMessage, len(list))
	for i := range list {
		t, ok := titles[list[i].Conv]
		if !ok {
			t = r.title(ctx, list[i].Conv)
			titles[list[i].Conv] = t
		}
		out[i] = toStored("", &list[i], t)
	}
	return out, nil
}

func (r reader) Chat(ctx context.Context, jid string) (StoredChat, error) {
	c, err := r.db.GetConversation(ctx, jid)
	if err != nil {
		return StoredChat{}, err
	}
	return toChat(c), nil
}

func (r reader) Chats(ctx context.Context, query string, limit, offset int) ([]StoredChat, error) {
	list, err := r.db.ListConversations(ctx, query, limit, offset)
	if err != nil {
		return nil, err
	}
	out := make([]StoredChat, len(list))
	for i := range list {
		out[i] = toChat(&list[i])
	}
	return out, nil
}

func unixTime(sec int64) time.Time {
	if sec == 0 {
		return time.Time{}
	}
	return time.Unix(sec, 0)
}

func unixOf(t time.Time) int64 {
	if t.IsZero() {
		return 0
	}
	return t.Unix()
}

func toStored(origin string, m *store.Message, title string) StoredMessage {
	return StoredMessage{
		Origin: origin, Chat: m.Conv, ChatTitle: title, ID: m.ID, Author: m.Author, Outgoing: m.Outgoing,
		SentAt: unixTime(m.SentAt), Body: m.Body, Kind: m.Kind,
		AttachmentName: m.AttachmentName, Mime: m.Mime, Size: m.ByteSize, URL: m.CDNURL, DirectPath: m.DirectPath,
		MediaKey: m.MediaKey, FileSHA256: m.PlainSHA256, FileEncSHA256: m.EncSHA256,
		ReplyToID: m.ReplyToID, ReplyToAuthor: m.ReplyToAuthor, ReplyPreview: m.ReplyPreview, Mentioned: m.Mentioned,
		EditedAt: unixTime(m.EditedAt), DeletedAt: unixTime(m.DeletedAt), DeletedUnverified: m.DeletedUnverified != 0,
	}
}

func toChat(c *store.Conversation) StoredChat {
	return StoredChat{JID: c.JID, Title: c.Title, IsGroup: c.IsGroup, LastActivity: unixTime(c.LastActivity)}
}
