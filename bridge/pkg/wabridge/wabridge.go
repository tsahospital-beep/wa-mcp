// Package wabridge runs the wa-mcp bridge inside another Go program. The
// program can watch every stored message and chat, add its own HTTP routes
// to the bridge API, and use the bridge's sending pipeline, media download,
// history requests and database from those routes.
//
// The wa-bridge command is a thin wrapper around Main. See
// docs/guides/embedding.md for an example.
package wabridge

import (
	"context"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

// Origins of a StoredMessage.
const (
	OriginLive    = store.OriginLive    // a message event while connected, also our own messages from the phone
	OriginHistory = store.OriginHistory // a message from a history sync
	OriginSent    = store.OriginSent    // a message this bridge sent
	OriginEdit    = store.OriginEdit    // an edit was applied; Body is the new text
	OriginDelete  = store.OriginDelete  // a deletion was recorded; DeletedAt is set
)

// Message kinds (StoredMessage.Kind).
const (
	KindText        = store.KindText
	KindImage       = store.KindImage
	KindVideo       = store.KindVideo
	KindAudio       = store.KindAudio
	KindVoice       = store.KindVoice
	KindDocument    = store.KindDocument
	KindSticker     = store.KindSticker
	KindLocation    = store.KindLocation
	KindContact     = store.KindContact
	KindPoll        = store.KindPoll
	KindUnsupported = store.KindUnsupported
)

// Errors returned by Deps. Compare with errors.Is.
var (
	ErrNotFound         = store.ErrNotFound
	ErrSendingDisabled  = sender.ErrDisabled
	ErrNotConnected     = wa.ErrNotConnected
	ErrNotLoggedIn      = wa.ErrNotLoggedIn
	ErrBadChat          = wa.ErrBadTarget
	ErrNotMedia         = media.ErrNotMedia
	ErrNoProfilePicture = media.ErrNoAvatar
)

// IsLimit reports whether a send was refused by sending.limits.
func IsLimit(err error) bool {
	var le *sender.LimitError
	return errors.As(err, &le)
}

// StoredMessage is a message row as the bridge stored it. Times are in the
// local time zone; zero times mean "not set".
type StoredMessage struct {
	// Origin tells what wrote the row (one of the Origin constants); empty
	// for rows read through Deps.Store.
	Origin    string
	Chat      string // chat JID
	ChatTitle string // title of the chat when the row was written ("" for a chat not stored yet)
	ID        string
	Author    string // author JID; our own JID for outgoing messages
	Outgoing  bool
	SentAt    time.Time
	Body      string // text or caption; kept for deleted messages
	Kind      string

	AttachmentName string // document file name, or sticker_<hash>.webp for stickers
	Mime           string
	Size           int64
	URL            string // CDN URL as received
	DirectPath     string
	MediaKey       []byte
	FileSHA256     []byte
	FileEncSHA256  []byte

	ReplyToID     string
	ReplyToAuthor string
	ReplyPreview  string // short text of the quoted message (at most 200 characters)
	Mentioned     []string

	EditedAt  time.Time
	DeletedAt time.Time
	// DeletedUnverified is set when a group member who is not the author
	// deleted the message and is not known to be an admin.
	DeletedUnverified bool
}

// StoredChat is a chat row as the bridge stored it.
type StoredChat struct {
	JID          string
	Title        string
	IsGroup      bool
	LastActivity time.Time
}

// Router registers extra HTTP routes on the bridge API. Patterns use the
// net/http ServeMux syntax ("/api/x" or "POST /api/x"); "/", "/health" and
// everything under /api/v1 belong to the bridge, and a pattern that could
// match one of them, also through a wildcard such as "/{p...}", is refused.
type Router interface {
	// Handle adds a route that needs the API bearer token, like /api/v1.
	Handle(pattern string, h http.Handler) error
	// HandleWithoutToken adds a route that skips the token check for callers
	// on a loopback address; callers from other addresses still need the
	// token. The Host check and the body size limit apply. Any program on
	// this machine can call it, so the handler must check its callers itself.
	HandleWithoutToken(pattern string, h http.Handler) error
}

// Options configures Run and Main. All fields are optional.
type Options struct {
	// ConfigPath is the config file. Empty means WA_MCP_CONFIG, then
	// ./config.json. Main's --config flag replaces it.
	ConfigPath string
	// SessionFile replaces session_file: the name of the whatsmeow session
	// database inside store_dir (default session.db).
	SessionFile string
	// ConfigSections are extra top-level config.json sections of the
	// embedding program: each value is a pointer the section is decoded
	// into. These keys are not reported as unknown.
	ConfigSections map[string]any
	// Version is printed by --version and reported by /api/v1/status.
	Version string
	// Stdout receives the log; Stderr receives usage and command errors.
	// They default to os.Stdout and os.Stderr.
	Stdout, Stderr io.Writer

	// OnMessageStored is called after a message row was written: new
	// messages, history, our own sends, edits and deletions. ctx carries
	// the values of the context that caused the write, for sends the one
	// given to Deps.Send. It runs on the goroutine that stored the row, so
	// keep it short; a panic is logged and does not stop the bridge.
	OnMessageStored func(ctx context.Context, m StoredMessage)
	// OnChatStored is called after a chat row was created, renamed or its
	// last activity moved forward. Same rules as OnMessageStored.
	OnChatStored func(ctx context.Context, c StoredChat)
	// Routes is called once before the API starts, to add HTTP routes. An
	// error stops the bridge.
	Routes func(r Router, d Deps) error
}
