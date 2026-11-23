package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
)

// ImportStats summarises a legacy import.
type ImportStats struct {
	Conversations int
	Messages      int // rows actually inserted
	Skipped       int // already present, empty or unreadable
}

// LegacyOptions tune ImportLegacy.
type LegacyOptions struct {
	// SessionDB is an older whatsmeow session database; its LID map tells
	// which bare sender ids are LIDs. Optional.
	SessionDB string
	// Batch is the number of rows per commit (default 500).
	Batch int
}

// ImportLegacy copies an older bridge database (tables "chats" and "messages"
// with text timestamps) into this store. Rows that already exist are kept.
// The bridge must not be running on this store while it runs.
func (d *DB) ImportLegacy(ctx context.Context, from string, opts LegacyOptions) (ImportStats, error) {
	var st ImportStats
	if opts.Batch <= 0 {
		opts.Batch = 500
	}
	abs, err := filepath.Abs(from)
	if err != nil {
		return st, err
	}
	if _, err := os.Stat(abs); err != nil {
		return st, err
	}
	old, err := sql.Open("sqlite", readOnlyURI(abs))
	if err != nil {
		return st, err
	}
	defer old.Close()

	chatCols, err := tableColumns(ctx, old, "chats")
	if err != nil {
		return st, err
	}
	msgCols, err := tableColumns(ctx, old, "messages")
	if err != nil {
		return st, err
	}
	if len(msgCols) == 0 {
		return st, fmt.Errorf("%s has no messages table; is it an older bridge database?", from)
	}
	lids, err := legacyLIDs(ctx, old, chatCols, opts.SessionDB)
	if err != nil {
		return st, err
	}

	latest := map[string]int64{}
	if err := copyLegacyMessages(ctx, d, old, msgCols, lids, opts.Batch, latest, &st); err != nil {
		return st, err
	}
	return st, copyLegacyChats(ctx, d, old, chatCols, latest, &st)
}

// legacyLIDs collects user ids known to be LIDs: chats addressed as @lid and
// entries of the whatsmeow LID map in an older session database.
func legacyLIDs(ctx context.Context, old *sql.DB, chatCols map[string]bool, sessionDB string) (map[string]bool, error) {
	lids := map[string]bool{}
	if len(chatCols) > 0 {
		rows, err := old.QueryContext(ctx, `SELECT jid FROM chats WHERE jid LIKE '%@lid'`)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var jid string
			if rows.Scan(&jid) == nil {
				lids[strings.TrimSuffix(jid, "@lid")] = true
			}
		}
		rows.Close()
	}
	if sessionDB == "" {
		return lids, nil
	}
	sess, err := sql.Open("sqlite", readOnlyURI(sessionDB))
	if err != nil {
		return nil, err
	}
	defer sess.Close()
	cols, err := tableColumns(ctx, sess, "whatsmeow_lid_map")
	if err != nil || !cols["lid"] {
		return lids, err
	}
	rows, err := sess.QueryContext(ctx, `SELECT lid FROM whatsmeow_lid_map`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var lid string
		if rows.Scan(&lid) == nil {
			lids[lid] = true
		}
	}
	return lids, rows.Err()
}

func copyLegacyMessages(ctx context.Context, d *DB, old *sql.DB, cols map[string]bool, lids map[string]bool,
	batch int, latest map[string]int64, st *ImportStats) error {
	pick := func(name string) string {
		if cols[name] {
			return name
		}
		return "NULL"
	}
	fields := []string{"id", "chat_jid", "sender", "content", "timestamp", "is_from_me", "media_type", "filename",
		"url", "media_key", "file_sha256", "file_enc_sha256", "file_length", "quoted_id", "quoted_text"}
	sel := make([]string, len(fields))
	for i, f := range fields {
		sel[i] = pick(f)
	}
	rows, err := old.QueryContext(ctx, "SELECT "+strings.Join(sel, ", ")+" FROM messages")
	if err != nil {
		return err
	}
	defer rows.Close()

	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { tx.Rollback() }()
	inBatch := 0
	for rows.Next() {
		var (
			id, conv, sender, content, kind, name, link, quotedID, quotedText sql.NullString
			ts, fromMe                                                        any
			key, plainHash, encHash                                           []byte
			size                                                              sql.NullInt64
		)
		if err := rows.Scan(&id, &conv, &sender, &content, &ts, &fromMe, &kind, &name, &link,
			&key, &plainHash, &encHash, &size, &quotedID, &quotedText); err != nil {
			return err
		}
		sentAt, ok := legacyTime(ts)
		if !ok || id.String == "" || conv.String == "" || (content.String == "" && kind.String == "") {
			st.Skipped++
			continue
		}
		m := &Message{
			Conv:           conv.String,
			ID:             id.String,
			Author:         legacyAuthor(sender.String, conv.String, lids),
			Outgoing:       legacyBool(fromMe),
			SentAt:         sentAt,
			Body:           content.String,
			Kind:           legacyKind(kind.String),
			AttachmentName: name.String,
			ByteSize:       size.Int64,
			CDNURL:         link.String,
			DirectPath:     common.CDNPath(link.String),
			MediaKey:       key,
			PlainSHA256:    plainHash,
			EncSHA256:      encHash,
			ReplyToID:      quotedID.String,
			ReplyPreview:   quotedText.String,
		}
		n, err := d.putMessage(ctx, tx, m, true)
		if err != nil {
			return err
		}
		if n > 0 {
			st.Messages++
		} else {
			st.Skipped++
		}
		if sentAt > latest[m.Conv] {
			latest[m.Conv] = sentAt
		}
		if inBatch++; inBatch >= batch {
			if err := tx.Commit(); err != nil {
				return err
			}
			if tx, err = d.sql.BeginTx(ctx, nil); err != nil {
				return err
			}
			inBatch = 0
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	return tx.Commit()
}

func copyLegacyChats(ctx context.Context, d *DB, old *sql.DB, cols map[string]bool, latest map[string]int64, st *ImportStats) error {
	type chatRow struct {
		jid, title string
		at         int64
	}
	var list []chatRow
	seen := map[string]bool{}
	if len(cols) > 0 {
		nameCol, timeCol := "NULL", "NULL"
		if cols["name"] {
			nameCol = "name"
		}
		if cols["last_message_time"] {
			timeCol = "last_message_time"
		}
		rows, err := old.QueryContext(ctx, "SELECT jid, "+nameCol+", "+timeCol+" FROM chats")
		if err != nil {
			return err
		}
		for rows.Next() {
			var jid, title sql.NullString
			var ts any
			if err := rows.Scan(&jid, &title, &ts); err != nil {
				rows.Close()
				return err
			}
			at, _ := legacyTime(ts)
			if jid.String != "" {
				list = append(list, chatRow{jid.String, title.String, at})
				seen[jid.String] = true
			}
		}
		rows.Close()
	}
	for jid, at := range latest {
		if !seen[jid] {
			list = append(list, chatRow{jid: jid, at: at})
		}
	}
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, c := range list {
		at := max(c.at, latest[c.jid])
		if _, err := tx.ExecContext(ctx, `INSERT INTO conversations (jid, title, is_group, last_activity) VALUES (?, ?, ?, ?)
			ON CONFLICT (jid) DO UPDATE SET
			  title = CASE WHEN conversations.title = '' THEN excluded.title ELSE conversations.title END,
			  last_activity = MAX(conversations.last_activity, excluded.last_activity)`,
			c.jid, c.title, boolInt(strings.HasSuffix(c.jid, "@g.us")), at); err != nil {
			return err
		}
		st.Conversations++
	}
	return tx.Commit()
}

func readOnlyURI(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	p := filepath.ToSlash(abs)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	p = strings.NewReplacer("%", "%25", "?", "%3f", "#", "%23").Replace(p)
	return "file://" + p + "?mode=ro"
}

func tableColumns(ctx context.Context, db *sql.DB, table string) (map[string]bool, error) {
	rows, err := db.QueryContext(ctx, "SELECT name FROM pragma_table_info(?)", table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		cols[strings.ToLower(name)] = true
	}
	return cols, rows.Err()
}

var legacyLayouts = []string{
	"2006-01-02 15:04:05.999999999-07:00",
	"2006-01-02T15:04:05.999999999-07:00",
	"2006-01-02 15:04:05.999999999 -0700 MST",
	"2006-01-02 15:04:05.999999999 -0700",
	time.RFC3339Nano,
	"2006-01-02 15:04:05.999999999",
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04",
	"2006-01-02",
}

// legacyTime turns the values older bridges stored as timestamps into unix seconds.
func legacyTime(v any) (int64, bool) {
	switch t := v.(type) {
	case time.Time:
		if t.IsZero() {
			return 0, false
		}
		return t.Unix(), true
	case int64:
		return unixGuess(t), t > 0
	case float64:
		return unixGuess(int64(t)), t > 0
	case []byte:
		return legacyTime(string(t))
	case string:
		s := strings.TrimSpace(t)
		if i := strings.Index(s, " m="); i > 0 {
			s = s[:i]
		}
		if n, err := strconv.ParseInt(s, 10, 64); err == nil {
			return unixGuess(n), n > 0
		}
		for _, layout := range legacyLayouts {
			if p, err := time.Parse(layout, s); err == nil {
				return p.Unix(), true
			}
		}
	}
	return 0, false
}

// unixGuess accepts seconds or milliseconds.
func unixGuess(n int64) int64 {
	if n > 100_000_000_000 {
		return n / 1000
	}
	return n
}

func legacyBool(v any) bool {
	switch b := v.(type) {
	case bool:
		return b
	case int64:
		return b != 0
	case string:
		return b == "1" || strings.EqualFold(b, "true")
	case []byte:
		return string(b) == "1" || strings.EqualFold(string(b), "true")
	}
	return false
}

func legacyKind(k string) string {
	switch {
	case k == "":
		return KindText
	case IsAttachment(k):
		return k
	default:
		return KindDocument
	}
}

// legacyAuthor adds a server part to bare user ids: @lid for ids known as
// LIDs or in LID chats, otherwise the phone-number server.
func legacyAuthor(sender, conv string, lids map[string]bool) string {
	if sender == "" || strings.Contains(sender, "@") {
		return sender
	}
	if strings.HasSuffix(conv, "@lid") || lids[sender] {
		return sender + "@lid"
	}
	return sender + "@s.whatsapp.net"
}
