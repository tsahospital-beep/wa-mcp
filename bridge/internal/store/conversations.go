package store

import (
	"context"
	"database/sql"
	"errors"
	"strings"
)

// Conversation is one row of conversations.
type Conversation struct {
	JID          string `json:"jid"`
	Title        string `json:"title"`
	IsGroup      bool   `json:"is_group"`
	LastActivity int64  `json:"last_activity"`
}

// TouchConversation creates the chat or updates it: a non-empty title replaces
// the old one, last_activity only moves forward.
func (d *DB) TouchConversation(ctx context.Context, c Conversation) error {
	_, err := d.sql.ExecContext(ctx, `INSERT INTO conversations (jid, title, is_group, last_activity)
		VALUES (?, ?, ?, ?)
		ON CONFLICT (jid) DO UPDATE SET
		  title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE conversations.title END,
		  is_group = MAX(conversations.is_group, excluded.is_group),
		  last_activity = MAX(conversations.last_activity, excluded.last_activity)`,
		c.JID, c.Title, boolInt(c.IsGroup), c.LastActivity)
	if err == nil {
		d.notifyConversation(ctx, c.JID)
	}
	return err
}

// GetConversation returns one chat or ErrNotFound.
func (d *DB) GetConversation(ctx context.Context, jid string) (*Conversation, error) {
	var c Conversation
	var group int
	err := d.sql.QueryRowContext(ctx, `SELECT jid, title, is_group, last_activity FROM conversations WHERE jid = ?`, jid).
		Scan(&c.JID, &c.Title, &group, &c.LastActivity)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	c.IsGroup = group != 0
	return &c, err
}

// ListConversations returns chats by recent activity, optionally filtered by title or JID.
func (d *DB) ListConversations(ctx context.Context, query string, limit, offset int) ([]Conversation, error) {
	if limit <= 0 || limit > 1000 {
		limit = 50
	}
	if offset < 0 {
		offset = 0
	}
	stmt := `SELECT jid, title, is_group, last_activity FROM conversations`
	var args []any
	if q := Fold(strings.TrimSpace(query)); q != "" {
		stmt += ` WHERE instr(wa_fold(title), ?) > 0 OR instr(jid, ?) > 0`
		args = append(args, q, q)
	}
	stmt += ` ORDER BY last_activity DESC, jid ASC LIMIT ? OFFSET ?`
	args = append(args, limit, offset)
	rows, err := d.sql.QueryContext(ctx, stmt, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Conversation{}
	for rows.Next() {
		var c Conversation
		var group int
		if err := rows.Scan(&c.JID, &c.Title, &group, &c.LastActivity); err != nil {
			return nil, err
		}
		c.IsGroup = group != 0
		out = append(out, c)
	}
	return out, rows.Err()
}

// UntitledDirectChats lists the stored chats that could use a contact name.
func (d *DB) UntitledDirectChats(ctx context.Context) ([]string, error) {
	rows, err := d.sql.QueryContext(ctx, "SELECT jid FROM conversations WHERE is_group = 0 AND title = ''")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var chats []string
	for rows.Next() {
		var jid string
		if err := rows.Scan(&jid); err != nil {
			return nil, err
		}
		chats = append(chats, jid)
	}
	return chats, rows.Err()
}
