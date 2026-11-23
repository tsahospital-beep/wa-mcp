package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
)

// Message is one row of chat_messages.
type Message struct {
	Conv           string   `json:"conv"`
	ID             string   `json:"msg_id"`
	Author         string   `json:"author"`
	Outgoing       bool     `json:"outgoing"`
	SentAt         int64    `json:"sent_at"`
	Body           string   `json:"body"`
	Kind           string   `json:"kind"`
	AttachmentName string   `json:"attachment_name,omitempty"`
	Mime           string   `json:"mime,omitempty"`
	ByteSize       int64    `json:"byte_size,omitempty"`
	DirectPath     string   `json:"-"`
	CDNURL         string   `json:"-"`
	MediaKey       []byte   `json:"-"`
	PlainSHA256    []byte   `json:"-"`
	EncSHA256      []byte   `json:"-"`
	LocalPath      string   `json:"local_path,omitempty"`
	ReplyToID      string   `json:"reply_to_id,omitempty"`
	ReplyToAuthor  string   `json:"reply_to_author,omitempty"`
	ReplyPreview   string   `json:"reply_preview,omitempty"`
	Mentioned      []string `json:"mentioned,omitempty"`
	ReadAt         int64    `json:"read_at,omitempty"`
	EditedAt       int64    `json:"edited_at,omitempty"`
	DeletedAt      int64    `json:"deleted_at,omitempty"`
	// DeletedUnverified is set when someone other than the author deleted the
	// message and their right to do so (group admin) is not confirmed; the
	// text stays visible.
	DeletedUnverified int64  `json:"deleted_unverified,omitempty"`
	DeletedBy         string `json:"deleted_by,omitempty"`
}

// IsMedia reports whether the message carries a downloadable attachment.
func (m *Message) IsMedia() bool { return IsAttachment(m.Kind) }

const messageColumns = `conv, msg_id, author, outgoing, sent_at, body, kind, attachment_name, mime, byte_size,
	direct_path, cdn_url, media_key, plain_sha256, enc_sha256, local_path,
	reply_to_id, reply_to_author, reply_preview, mentioned, read_at, edited_at, deleted_at,
	deleted_unverified, deleted_by`

// execer is satisfied by *sql.DB and *sql.Tx.
type execer interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}

// PutMessage inserts or updates a message. On an update the author, the
// direction and the send time never change (callers check that the author
// matches first); known local state (local_path, read_at, edits, deletion)
// is kept and new non-empty values replace old ones. The body of an edited
// message is only changed by a newer edit.
func (d *DB) PutMessage(ctx context.Context, m *Message) error {
	_, err := d.putMessage(ctx, d.sql, m, false)
	if err == nil {
		d.notifyMessage(ctx, OriginOf(ctx), m.Conv, m.ID)
	}
	return err
}

// putMessage returns the number of rows written (0 when keepExisting skipped a duplicate).
func (d *DB) putMessage(ctx context.Context, ex execer, m *Message, keepExisting bool) (int64, error) {
	if m.Conv == "" || m.ID == "" {
		return 0, errors.New("message needs conv and id")
	}
	kind := m.Kind
	if kind == "" {
		kind = KindText
	}
	body, err := d.sealText(m.Body, field{"chat_messages", "body", m.Conv, m.ID})
	if err != nil {
		return 0, err
	}
	name, err := d.sealText(m.AttachmentName, field{"chat_messages", "attachment_name", m.Conv, m.ID})
	if err != nil {
		return 0, err
	}
	preview, err := d.sealText(m.ReplyPreview, field{"chat_messages", "reply_preview", m.Conv, m.ID})
	if err != nil {
		return 0, err
	}
	key, err := d.sealBlob(m.MediaKey, field{"chat_messages", "media_key", m.Conv, m.ID})
	if err != nil {
		return 0, err
	}
	var mentioned any
	if len(m.Mentioned) > 0 {
		b, _ := json.Marshal(m.Mentioned)
		mentioned = string(b)
	}
	conflict := `ON CONFLICT (conv, msg_id) DO UPDATE SET
		author = CASE WHEN chat_messages.author = '' THEN excluded.author ELSE chat_messages.author END,
		body = CASE WHEN chat_messages.edited_at IS NOT NULL THEN chat_messages.body
		            WHEN excluded.body <> '' THEN excluded.body ELSE chat_messages.body END,
		kind = excluded.kind,
		attachment_name = COALESCE(excluded.attachment_name, chat_messages.attachment_name),
		mime = COALESCE(excluded.mime, chat_messages.mime),
		byte_size = COALESCE(excluded.byte_size, chat_messages.byte_size),
		direct_path = COALESCE(excluded.direct_path, chat_messages.direct_path),
		cdn_url = COALESCE(excluded.cdn_url, chat_messages.cdn_url),
		media_key = COALESCE(excluded.media_key, chat_messages.media_key),
		plain_sha256 = COALESCE(excluded.plain_sha256, chat_messages.plain_sha256),
		enc_sha256 = COALESCE(excluded.enc_sha256, chat_messages.enc_sha256),
		local_path = COALESCE(chat_messages.local_path, excluded.local_path),
		reply_to_id = COALESCE(excluded.reply_to_id, chat_messages.reply_to_id),
		reply_to_author = COALESCE(excluded.reply_to_author, chat_messages.reply_to_author),
		reply_preview = COALESCE(excluded.reply_preview, chat_messages.reply_preview),
		mentioned = COALESCE(excluded.mentioned, chat_messages.mentioned),
		read_at = COALESCE(chat_messages.read_at, excluded.read_at),
		edited_at = COALESCE(chat_messages.edited_at, excluded.edited_at),
		deleted_at = COALESCE(chat_messages.deleted_at, excluded.deleted_at)`
	if keepExisting {
		conflict = `ON CONFLICT (conv, msg_id) DO NOTHING`
	}
	res, err := ex.ExecContext(ctx, `INSERT INTO chat_messages (`+messageColumns+`)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL) `+conflict,
		m.Conv, m.ID, m.Author, boolInt(m.Outgoing), m.SentAt, body, kind,
		nullText(name), nullText(m.Mime), nullInt(m.ByteSize),
		nullText(m.DirectPath), nullText(m.CDNURL), nullBlob(key), nullBlob(m.PlainSHA256), nullBlob(m.EncSHA256),
		nullText(m.LocalPath), nullText(m.ReplyToID), nullText(m.ReplyToAuthor), nullText(preview),
		mentioned, nullInt(m.ReadAt), nullInt(m.EditedAt), nullInt(m.DeletedAt))
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

type scanner interface {
	Scan(dest ...any) error
}

// scanMessage reads one row; deleted messages lose their text unless showDeleted.
func (d *DB) scanMessage(row scanner, showDeleted bool) (*Message, error) {
	var (
		m                                             Message
		out                                           int
		name, mime, dpath, url, local, rid, rauth, rp sql.NullString
		mentioned, deletedBy                          sql.NullString
		size, readAt, editedAt, deletedAt, unverified sql.NullInt64
		key                                           []byte
	)
	if err := row.Scan(&m.Conv, &m.ID, &m.Author, &out, &m.SentAt, &m.Body, &m.Kind, &name, &mime, &size,
		&dpath, &url, &key, &m.PlainSHA256, &m.EncSHA256, &local, &rid, &rauth, &rp, &mentioned, &readAt,
		&editedAt, &deletedAt, &unverified, &deletedBy); err != nil {
		return nil, err
	}
	m.Outgoing = out != 0
	m.Body = d.openText(m.Body, field{"chat_messages", "body", m.Conv, m.ID})
	m.AttachmentName = d.openText(name.String, field{"chat_messages", "attachment_name", m.Conv, m.ID})
	m.ReplyPreview = d.openText(rp.String, field{"chat_messages", "reply_preview", m.Conv, m.ID})
	m.MediaKey = d.openBlob(key, field{"chat_messages", "media_key", m.Conv, m.ID})
	m.Mime, m.ByteSize = mime.String, size.Int64
	m.DirectPath, m.CDNURL, m.LocalPath = dpath.String, url.String, local.String
	m.ReplyToID, m.ReplyToAuthor = rid.String, rauth.String
	m.ReadAt, m.EditedAt, m.DeletedAt = readAt.Int64, editedAt.Int64, deletedAt.Int64
	m.DeletedUnverified, m.DeletedBy = unverified.Int64, deletedBy.String
	if mentioned.Valid && mentioned.String != "" {
		_ = json.Unmarshal([]byte(mentioned.String), &m.Mentioned)
	}
	if m.DeletedAt != 0 && !showDeleted {
		m.Body, m.ReplyPreview = "", ""
	}
	return &m, nil
}

// GetMessage returns one message or ErrNotFound (text hidden when deleted).
func (d *DB) GetMessage(ctx context.Context, conv, id string) (*Message, error) {
	row := d.sql.QueryRowContext(ctx, `SELECT `+messageColumns+` FROM chat_messages WHERE conv = ? AND msg_id = ?`, conv, id)
	m, err := d.scanMessage(row, false)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return m, err
}

// OldestMessage returns the earliest stored message of a chat.
func (d *DB) OldestMessage(ctx context.Context, conv string) (*Message, error) {
	row := d.sql.QueryRowContext(ctx, `SELECT `+messageColumns+` FROM chat_messages WHERE conv = ?
		ORDER BY sent_at ASC, msg_id ASC LIMIT 1`, conv)
	m, err := d.scanMessage(row, false)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return m, err
}

// MessageQuery filters ListMessages. Zero values mean "no filter".
type MessageQuery struct {
	Conv           string
	After          int64 // sent_at > After
	Before         int64 // sent_at < Before
	Query          string
	Limit          int
	Offset         int
	Ascending      bool
	IncludeDeleted bool // show (and search) the text of deleted messages
}

// searchPage is how many rows the sealed-store search reads at a time.
var searchPage = 500

// ListMessages returns messages, newest first unless Ascending is set.
// Text search uses full Unicode case folding: in SQL on plain databases,
// page by page after decryption on sealed ones. Deleted messages are not
// searched unless IncludeDeleted is set.
func (d *DB) ListMessages(ctx context.Context, q MessageQuery) ([]Message, error) {
	if q.Limit <= 0 || q.Limit > 1000 {
		q.Limit = 100
	}
	if q.Offset < 0 {
		q.Offset = 0
	}
	var where []string
	var args []any
	if q.Conv != "" {
		where = append(where, "conv = ?")
		args = append(args, q.Conv)
	}
	if q.After > 0 {
		where = append(where, "sent_at > ?")
		args = append(args, q.After)
	}
	if q.Before > 0 {
		where = append(where, "sent_at < ?")
		args = append(args, q.Before)
	}
	needle := Fold(strings.TrimSpace(q.Query))
	if needle != "" && !q.IncludeDeleted {
		where = append(where, "deleted_at IS NULL")
	}
	sealedSearch := needle != "" && d.Encrypted()
	if needle != "" && !sealedSearch {
		where = append(where, "(instr(wa_fold(body), ?) > 0 OR instr(wa_fold(COALESCE(attachment_name, '')), ?) > 0)")
		args = append(args, needle, needle)
	}
	stmt := `SELECT ` + messageColumns + ` FROM chat_messages`
	if len(where) > 0 {
		stmt += " WHERE " + strings.Join(where, " AND ")
	}
	order := "DESC"
	if q.Ascending {
		order = "ASC"
	}
	stmt += " ORDER BY sent_at " + order + ", msg_id " + order + " LIMIT ? OFFSET ?"
	if !sealedSearch {
		return d.queryMessages(ctx, stmt, q.IncludeDeleted, append(args, q.Limit, q.Offset)...)
	}

	out := []Message{}
	skipped := 0
	for offset := 0; ; offset += searchPage {
		page, err := d.queryMessages(ctx, stmt, q.IncludeDeleted, append(args, searchPage, offset)...)
		if err != nil {
			return nil, err
		}
		for _, m := range page {
			if !strings.Contains(Fold(m.Body), needle) && !strings.Contains(Fold(m.AttachmentName), needle) {
				continue
			}
			if skipped < q.Offset {
				skipped++
				continue
			}
			out = append(out, m)
			if len(out) >= q.Limit {
				return out, nil
			}
		}
		if len(page) < searchPage {
			return out, nil
		}
	}
}

func (d *DB) queryMessages(ctx context.Context, stmt string, showDeleted bool, args ...any) ([]Message, error) {
	rows, err := d.sql.QueryContext(ctx, stmt, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Message{}
	for rows.Next() {
		m, err := d.scanMessage(rows, showDeleted)
		if err != nil {
			return nil, err
		}
		out = append(out, *m)
	}
	return out, rows.Err()
}

// UnreadIncoming returns incoming messages newer than our last outgoing
// message in conv that we have not marked read yet, newest first.
func (d *DB) UnreadIncoming(ctx context.Context, conv string, limit int) ([]Message, error) {
	if limit <= 0 {
		limit = 20
	}
	return d.queryMessages(ctx, `SELECT `+messageColumns+` FROM chat_messages
		WHERE conv = ? AND outgoing = 0 AND read_at IS NULL
		  AND sent_at >= (SELECT COALESCE(MAX(sent_at), 0) FROM chat_messages WHERE conv = ? AND outgoing = 1)
		ORDER BY sent_at DESC LIMIT ?`, false, conv, conv, limit)
}

// MarkRead stores the time we marked messages read (only for rows not marked yet).
func (d *DB) MarkRead(ctx context.Context, conv string, ids []string, at int64) error {
	if len(ids) == 0 {
		return nil
	}
	args := []any{at, conv}
	for _, id := range ids {
		args = append(args, id)
	}
	_, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET read_at = ?
		WHERE conv = ? AND read_at IS NULL AND msg_id IN (`+placeholders(len(ids))+`)`, args...)
	return err
}

// SetLocalPath records where the attachment was saved.
func (d *DB) SetLocalPath(ctx context.Context, conv, id, path string) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET local_path = ? WHERE conv = ? AND msg_id = ?`, path, conv, id)
	return err
}

// SetDirectPath replaces the CDN path after the phone re-uploaded the media.
func (d *DB) SetDirectPath(ctx context.Context, conv, id, directPath string) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET direct_path = ? WHERE conv = ? AND msg_id = ?`, directPath, conv, id)
	return err
}

// EditBody stores an edit unless a newer one is already stored. It reports
// whether the row changed.
func (d *DB) EditBody(ctx context.Context, conv, id, body string, at int64) (bool, error) {
	sealed, err := d.sealText(body, field{"chat_messages", "body", conv, id})
	if err != nil {
		return false, err
	}
	res, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET body = ?, edited_at = ?
		WHERE conv = ? AND msg_id = ? AND (edited_at IS NULL OR edited_at <= ?)`, sealed, at, conv, id, at)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	if n > 0 {
		d.notifyMessage(ctx, OriginEdit, conv, id)
	}
	return n > 0, nil
}

// MarkDeleted records that a message was deleted for everyone. The text stays
// in the database but reads hide it unless asked.
func (d *DB) MarkDeleted(ctx context.Context, conv, id, by string, at int64) (bool, error) {
	res, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET deleted_at = ?, deleted_by = ?, deleted_unverified = NULL
		WHERE conv = ? AND msg_id = ? AND deleted_at IS NULL`, at, nullText(by), conv, id)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	if n > 0 {
		d.notifyMessage(ctx, OriginDelete, conv, id)
	}
	return n > 0, nil
}

// MarkDeletedUnverified records a deletion by someone whose right to delete
// is not confirmed; the text stays visible.
func (d *DB) MarkDeletedUnverified(ctx context.Context, conv, id, by string, at int64) (bool, error) {
	res, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET deleted_unverified = ?, deleted_by = ?
		WHERE conv = ? AND msg_id = ? AND deleted_at IS NULL`, at, nullText(by), conv, id)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	if n > 0 {
		d.notifyMessage(ctx, OriginDelete, conv, id)
	}
	return n > 0, nil
}

// VerifyDeletions turns unverified deletions in conv by any of admins (JID
// strings) into real deletions and returns how many changed.
func (d *DB) VerifyDeletions(ctx context.Context, conv string, admins []string) (int64, error) {
	if len(admins) == 0 {
		return 0, nil
	}
	args := []any{conv}
	for _, a := range admins {
		args = append(args, a)
	}
	res, err := d.sql.ExecContext(ctx, `UPDATE chat_messages SET deleted_at = deleted_unverified, deleted_unverified = NULL
		WHERE conv = ? AND deleted_unverified IS NOT NULL AND deleted_at IS NULL
		  AND deleted_by IN (`+placeholders(len(admins))+`)`, args...)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.Repeat("?, ", n-1) + "?"
}
