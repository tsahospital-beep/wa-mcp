package store

import "context"

// ImportedPrefix starts the id of every message that came from an exported chat file.
const ImportedPrefix = "imp-"

// ConversationsByTitle returns the chats whose title equals title when both are case folded.
func (d *DB) ConversationsByTitle(ctx context.Context, title string) ([]Conversation, error) {
	needle := Fold(title)
	if needle == "" {
		return nil, nil
	}
	rows, err := d.sql.QueryContext(ctx, `SELECT jid, title, is_group, last_activity FROM conversations
		WHERE wa_fold(title) = ? ORDER BY last_activity DESC, jid ASC LIMIT 20`, needle)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Conversation
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

// ScanMessages calls fn for every message of conv sent from..to (unix seconds,
// both included), oldest first, with deleted messages and their text.
func (d *DB) ScanMessages(ctx context.Context, conv string, from, to int64, fn func(*Message)) error {
	rows, err := d.sql.QueryContext(ctx, `SELECT `+messageColumns+` FROM chat_messages
		WHERE conv = ? AND sent_at >= ? AND sent_at <= ? ORDER BY sent_at ASC, msg_id ASC`, conv, from, to)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		m, err := d.scanMessage(rows, true)
		if err != nil {
			return err
		}
		fn(m)
	}
	return rows.Err()
}

// InsertMessages stores messages that are not stored yet (same chat and id) in
// transactions of batch rows and returns how many were new. Rows that exist
// are left as they are.
func (d *DB) InsertMessages(ctx context.Context, list []*Message, batch int) (int, error) {
	if batch <= 0 {
		batch = 500
	}
	added := 0
	for start := 0; start < len(list); start += batch {
		if err := ctx.Err(); err != nil {
			return added, err
		}
		end := min(start+batch, len(list))
		n, err := d.insertBatch(ctx, list[start:end])
		added += n
		if err != nil {
			return added, err
		}
	}
	return added, nil
}

func (d *DB) insertBatch(ctx context.Context, list []*Message) (int, error) {
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	n := 0
	for _, m := range list {
		rows, err := d.putMessage(ctx, tx, m, true)
		if err != nil {
			return 0, err
		}
		n += int(rows)
	}
	return n, tx.Commit()
}

// LinkImportedFile gives an imported message that has no file yet (an earlier
// import without the media) its kind, text, attachment details and file. It
// reports whether the row changed.
func (d *DB) LinkImportedFile(ctx context.Context, m *Message) (bool, error) {
	name, err := d.sealText(m.AttachmentName, field{"chat_messages", "attachment_name", m.Conv, m.ID})
	if err != nil {
		return false, err
	}
	body, err := d.sealText(m.Body, field{"chat_messages", "body", m.Conv, m.ID})
	if err != nil {
		return false, err
	}
	res, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET kind = ?, body = ?, attachment_name = ?, mime = ?, byte_size = ?, local_path = ?
		WHERE conv = ? AND msg_id = ? AND msg_id LIKE 'imp-%' AND local_path IS NULL`,
		m.Kind, body, nullText(name), nullText(m.Mime), nullInt(m.ByteSize), m.LocalPath, m.Conv, m.ID)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}
