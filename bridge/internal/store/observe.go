package store

import (
	"context"
	"database/sql"
	"errors"
	"sync/atomic"
)

// Origins of a message write, as reported to the Observer.
const (
	OriginLive    = "live"    // a message event while connected, including our own messages from other devices
	OriginHistory = "history" // a message from a history sync
	OriginSent    = "sent"    // a message this bridge sent
	OriginEdit    = "edit"    // an edit applied to a stored message
	OriginDelete  = "delete"  // a deletion recorded for a stored message
)

type originKey struct{}

// WithOrigin marks message writes made with ctx as coming from origin.
func WithOrigin(ctx context.Context, origin string) context.Context {
	return context.WithValue(ctx, originKey{}, origin)
}

// OriginOf returns the origin set with WithOrigin ("" when none).
func OriginOf(ctx context.Context) string {
	s, _ := ctx.Value(originKey{}).(string)
	return s
}

// Observer is told about rows after they were written. The functions run on
// the goroutine that wrote the row, with the writer's context. Either may be nil.
type Observer struct {
	// Message gets the stored row with its text even when it was deleted, and
	// its chat (nil when the chat row does not exist yet).
	Message func(ctx context.Context, origin string, m *Message, chat *Conversation)
	// Conversation gets a chat row after it was created, renamed or moved forward.
	Conversation func(ctx context.Context, c *Conversation)
}

// observerSlot holds the installed Observer.
type observerSlot = atomic.Pointer[Observer]

// SetObserver installs o (nil removes it). Set it before writes start.
func (d *DB) SetObserver(o *Observer) { d.observer.Store(o) }

func (d *DB) obs() *Observer { return d.observer.Load() }

// notifyMessage reports one message row to the observer.
func (d *DB) notifyMessage(ctx context.Context, origin, conv, id string) {
	o := d.obs()
	if o == nil || o.Message == nil {
		return
	}
	row := d.sql.QueryRowContext(ctx, `SELECT `+messageColumns+` FROM chat_messages WHERE conv = ? AND msg_id = ?`, conv, id)
	m, err := d.scanMessage(row, true)
	if err != nil {
		if !errors.Is(err, sql.ErrNoRows) {
			d.debugf("store: observer could not read message %s: %v", id, err)
		}
		return
	}
	chat, err := d.GetConversation(ctx, conv)
	if err != nil {
		chat = nil
	}
	o.Message(ctx, origin, m, chat)
}

// notifyConversation reports one chat row to the observer.
func (d *DB) notifyConversation(ctx context.Context, jid string) {
	o := d.obs()
	if o == nil || o.Conversation == nil {
		return
	}
	c, err := d.GetConversation(ctx, jid)
	if err != nil {
		return
	}
	o.Conversation(ctx, c)
}
