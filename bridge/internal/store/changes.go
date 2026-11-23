package store

import (
	"context"
	"database/sql"
	"time"
	"unicode/utf8"
)

// Pending change kinds.
const (
	ChangeEdit   = "edit"
	ChangeRevoke = "revoke"
)

// Limits for changes that arrive before (or without) their message.
const (
	PendingWindow   = 7 * 24 * time.Hour
	MaxPending      = 5000
	MaxReactionText = 32 // bytes
)

// PendingChange is an edit or deletion that arrived before its message.
type PendingChange struct {
	Conv     string
	ID       string
	Kind     string
	Actor    string
	ActorAlt string
	Body     string
	At       int64
}

// AddPending keeps the newest pending change of each kind per message.
// Entries older than PendingWindow and the oldest beyond MaxPending are dropped.
func (d *DB) AddPending(ctx context.Context, p PendingChange) error {
	body, err := d.sealText(p.Body, field{"pending_changes", "body", p.Conv, p.ID})
	if err != nil {
		return err
	}
	now := d.now().Unix()
	if _, err = d.sql.ExecContext(ctx, `INSERT INTO pending_changes (conv, msg_id, kind, actor, actor_alt, body, at, added_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (conv, msg_id, kind) DO UPDATE SET actor = excluded.actor, actor_alt = excluded.actor_alt,
		  body = excluded.body, at = excluded.at, added_at = excluded.added_at
		WHERE excluded.at >= pending_changes.at`,
		p.Conv, p.ID, p.Kind, p.Actor, p.ActorAlt, body, p.At, now); err != nil {
		return err
	}
	return d.prunePending(ctx, now)
}

func (d *DB) prunePending(ctx context.Context, now int64) error {
	if _, err := d.sql.ExecContext(ctx, `DELETE FROM pending_changes WHERE added_at < ?`,
		now-int64(PendingWindow/time.Second)); err != nil {
		return err
	}
	_, err := d.sql.ExecContext(ctx, `DELETE FROM pending_changes WHERE rowid IN (
		SELECT rowid FROM pending_changes ORDER BY added_at ASC, rowid ASC
		LIMIT MAX(0, (SELECT COUNT(*) FROM pending_changes) - ?))`, MaxPending)
	return err
}

// ApplyPending removes each change only after applying it successfully.
// Match the stored value so a concurrent edit remains available to the next call.
func (d *DB) ApplyPending(ctx context.Context, conv, id string, apply func(PendingChange) error) error {
	rows, err := d.sql.QueryContext(ctx, `SELECT kind, actor, actor_alt, body, at FROM pending_changes
		WHERE conv = ? AND msg_id = ? ORDER BY at`, conv, id)
	if err != nil {
		return err
	}
	var pending []PendingChange
	for rows.Next() {
		p := PendingChange{Conv: conv, ID: id}
		if err := rows.Scan(&p.Kind, &p.Actor, &p.ActorAlt, &p.Body, &p.At); err != nil {
			rows.Close()
			return err
		}
		pending = append(pending, p)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, p := range pending {
		raw := p.Body
		p.Body = d.openText(raw, field{"pending_changes", "body", conv, id})
		if err := apply(p); err != nil {
			return err
		}
		if _, err := d.sql.ExecContext(ctx, `DELETE FROM pending_changes WHERE conv=? AND msg_id=? AND kind=?
			AND actor=? AND actor_alt=? AND body=? AND at=?`, conv, id, p.Kind, p.Actor, p.ActorAlt, raw, p.At); err != nil {
			return err
		}
	}
	return nil
}

// Reaction is one person's reaction to a message.
type Reaction struct {
	Conv    string `json:"-"`
	MsgID   string `json:"-"`
	Reactor string `json:"reactor"`
	Emoji   string `json:"emoji"`
	At      int64  `json:"reacted_at"`
}

// capText cuts s to at most n bytes on a rune boundary.
func capText(s string, n int) string {
	if len(s) <= n {
		return s
	}
	s = s[:n]
	for len(s) > 0 && !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}

// SetReaction stores or (with an empty Emoji) removes a reaction; older
// updates than the stored one are ignored. The text is capped at
// MaxReactionText bytes. Reactions to messages that never arrived are
// dropped after PendingWindow.
func (d *DB) SetReaction(ctx context.Context, r Reaction) error {
	now := d.now().Unix()
	if r.Emoji == "" {
		_, err := d.sql.ExecContext(ctx, `DELETE FROM reactions WHERE conv = ? AND msg_id = ? AND reactor = ? AND reacted_at <= ?`,
			r.Conv, r.MsgID, r.Reactor, r.At)
		return err
	}
	emoji, err := d.sealText(capText(r.Emoji, MaxReactionText), field{"reactions", "emoji", r.Conv, r.MsgID})
	if err != nil {
		return err
	}
	if _, err = d.sql.ExecContext(ctx, `INSERT INTO reactions (conv, msg_id, reactor, emoji, reacted_at, added_at) VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT (conv, msg_id, reactor) DO UPDATE SET emoji = excluded.emoji, reacted_at = excluded.reacted_at
		WHERE excluded.reacted_at >= reactions.reacted_at`, r.Conv, r.MsgID, r.Reactor, emoji, r.At, now); err != nil {
		return err
	}
	return d.pruneReactions(ctx, now)
}

// Reaction pruning runs on every write, so it stays cheap: old orphans are
// found through the index on added_at and deleted in batches, and the count
// cap, which reads the whole table, runs at most once per capEvery.
const (
	pruneBatch = 500
	capEvery   = time.Minute
)

func (d *DB) pruneReactions(ctx context.Context, now int64) error {
	orphan := `NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.conv = reactions.conv AND m.msg_id = reactions.msg_id)`
	if _, err := d.sql.ExecContext(ctx, `DELETE FROM reactions WHERE rowid IN (
		SELECT rowid FROM reactions WHERE added_at < ? AND `+orphan+` ORDER BY added_at LIMIT ?)`,
		now-int64(PendingWindow/time.Second), pruneBatch); err != nil {
		return err
	}
	d.pruneMu.Lock()
	due := d.lastCapPrune == 0 || now-d.lastCapPrune >= int64(capEvery/time.Second)
	if due {
		d.lastCapPrune = now
	}
	d.pruneMu.Unlock()
	if !due {
		return nil
	}
	_, err := d.sql.ExecContext(ctx, `DELETE FROM reactions WHERE rowid IN (
		SELECT rowid FROM reactions WHERE `+orphan+` ORDER BY added_at ASC, rowid ASC
		LIMIT MAX(0, (SELECT COUNT(*) FROM reactions WHERE `+orphan+`) - ?))`, MaxPending)
	return err
}

// ReactionsFor returns the reactions to the given messages of one chat, by message id.
func (d *DB) ReactionsFor(ctx context.Context, conv string, ids []string) (map[string][]Reaction, error) {
	out := map[string][]Reaction{}
	if len(ids) == 0 {
		return out, nil
	}
	args := []any{conv}
	for _, id := range ids {
		args = append(args, id)
	}
	rows, err := d.sql.QueryContext(ctx, `SELECT msg_id, reactor, emoji, reacted_at FROM reactions
		WHERE conv = ? AND msg_id IN (`+placeholders(len(ids))+`) ORDER BY reacted_at`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		r := Reaction{Conv: conv}
		if err := rows.Scan(&r.MsgID, &r.Reactor, &r.Emoji, &r.At); err != nil {
			return nil, err
		}
		r.Emoji = d.openText(r.Emoji, field{"reactions", "emoji", r.Conv, r.MsgID})
		out[r.MsgID] = append(out[r.MsgID], r)
	}
	return out, rows.Err()
}

// SetTitle renames an existing chat; with force=false only an empty title is
// filled. It never creates a chat.
func (d *DB) SetTitle(ctx context.Context, jid, title string, force bool) (bool, error) {
	if title == "" {
		return false, nil
	}
	var res sql.Result
	var err error
	if force {
		res, err = d.sql.ExecContext(ctx, `UPDATE conversations SET title = ? WHERE jid = ? AND title <> ?`, title, jid, title)
	} else {
		res, err = d.sql.ExecContext(ctx, `UPDATE conversations SET title = ? WHERE jid = ? AND title = ''`, title, jid)
	}
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	if n > 0 {
		d.notifyConversation(ctx, jid)
	}
	return n > 0, nil
}
