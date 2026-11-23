package store

import (
	"database/sql"
	"path/filepath"
	"testing"
	"time"
)

// makeLegacyDB writes a database in the old layout: text timestamps,
// bare user ids as senders and optional reply columns.
func makeLegacyDB(t *testing.T, withQuotes bool) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "old messages.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	extra := ""
	if withQuotes {
		extra = ", quoted_id TEXT, quoted_text TEXT"
	}
	stmts := []string{
		`CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)`,
		`CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
			is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB,
			file_enc_sha256 BLOB, file_length INTEGER` + extra + `, PRIMARY KEY (id, chat_jid))`,
		`INSERT INTO chats VALUES ('15550100002@s.whatsapp.net', 'Bob', '2026-03-01 10:00:05+02:00')`,
		`INSERT INTO chats VALUES ('120363000000000001@g.us', 'Book club', '2026-03-02 09:30:00.123456789+00:00')`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			t.Fatal(err)
		}
	}
	ins := `INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url,
		media_key, file_sha256, file_enc_sha256, file_length) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
	rows := [][]any{
		{"M1", "15550100002@s.whatsapp.net", "15550100002", "Hi Alice", "2026-03-01 10:00:00+02:00", 0, "", "", "", nil, nil, nil, 0},
		{"M2", "15550100002@s.whatsapp.net", "15550100001", "Hi Bob", "2026-03-01T10:00:05+02:00", 1, "", "", "", nil, nil, nil, 0},
		{"M3", "120363000000000001@g.us", "15550100003", "", "2026-03-02 09:30:00.123456789+00:00", 0, "image", "image_1.jpg",
			"https://mmg.whatsapp.net/v/t62.7118-24/abc.enc?ccb=11-4&oh=sig&oe=1", []byte{1}, []byte{2}, []byte{3}, 2048},
		{"M4", "120363000000000001@g.us", "15550100003", "", "2026-03-02 09:31:00", 0, "", "", "", nil, nil, nil, 0},
		{"M5", "15550100002@s.whatsapp.net", "15550100002", "bad time", "not a time", 0, "", "", "", nil, nil, nil, 0},
	}
	for _, r := range rows {
		if _, err := db.Exec(ins, r...); err != nil {
			t.Fatal(err)
		}
	}
	if withQuotes {
		db.Exec(`UPDATE messages SET quoted_id = 'M1', quoted_text = 'Hi Alice' WHERE id = 'M2'`)
	}
	return path
}

func TestImportLegacy(t *testing.T) {
	src := makeLegacyDB(t, true)
	db := openTemp(t)

	// A row that already exists must not be overwritten.
	db.PutMessage(ctx, &Message{Conv: "15550100002@s.whatsapp.net", ID: "M1", Author: "x", SentAt: 1, Body: "newer copy"})

	st, err := db.ImportLegacy(ctx, src, LegacyOptions{})
	if err != nil {
		t.Fatal(err)
	}
	// M1 exists, M4 is empty, M5 has an unreadable time.
	if st.Messages != 2 || st.Skipped != 3 || st.Conversations != 2 {
		t.Fatalf("stats: %+v", st)
	}

	m1, _ := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "M1")
	if m1.Body != "newer copy" {
		t.Fatalf("existing row overwritten: %+v", m1)
	}
	m2, err := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "M2")
	if err != nil {
		t.Fatal(err)
	}
	want := time.Date(2026, 3, 1, 8, 0, 5, 0, time.UTC).Unix()
	if m2.SentAt != want || !m2.Outgoing || m2.Author != "15550100001@s.whatsapp.net" || m2.ReplyToID != "M1" || m2.ReplyPreview != "Hi Alice" {
		t.Fatalf("M2: %+v (want sent_at %d)", m2, want)
	}
	m3, _ := db.GetMessage(ctx, "120363000000000001@g.us", "M3")
	if m3.Kind != "image" || m3.AttachmentName != "image_1.jpg" || m3.ByteSize != 2048 ||
		m3.DirectPath != "/v/t62.7118-24/abc.enc?ccb=11-4&oh=sig&oe=1" || len(m3.MediaKey) != 1 {
		t.Fatalf("M3: %+v", m3)
	}

	c, _ := db.GetConversation(ctx, "120363000000000001@g.us")
	if c.Title != "Book club" || !c.IsGroup || c.LastActivity == 0 {
		t.Fatalf("group chat: %+v", c)
	}

	// Running the import again adds nothing.
	st, err = db.ImportLegacy(ctx, src, LegacyOptions{})
	if err != nil || st.Messages != 0 {
		t.Fatalf("second import: %+v %v", st, err)
	}
}

func TestImportLegacyWithoutQuoteColumns(t *testing.T) {
	src := makeLegacyDB(t, false)
	db := openTemp(t)
	if err := db.SetupEncryption(ctx, true, "pw"); err != nil {
		t.Fatal(err)
	}
	st, err := db.ImportLegacy(ctx, src, LegacyOptions{})
	if err != nil || st.Messages != 3 {
		t.Fatalf("%+v %v", st, err)
	}
	var raw string
	db.SQL().QueryRow(`SELECT body FROM chat_messages WHERE msg_id = 'M1'`).Scan(&raw)
	if len(raw) < 5 || raw[:5] != "wae2:" {
		t.Fatalf("imported body not sealed: %q", raw)
	}
	m, _ := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "M1")
	if m.Body != "Hi Alice" {
		t.Fatalf("decrypted body: %q", m.Body)
	}
}

func TestImportLegacyBatchesAndLIDAuthors(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "messages.db")
	old, _ := sql.Open("sqlite", src)
	old.Exec(`CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
		media_type TEXT, PRIMARY KEY (id, chat_jid))`)
	for i := 0; i < 7; i++ {
		sender := "15550100003"
		if i%2 == 1 {
			sender = "99990000000123" // a LID in the old session
		}
		old.Exec(`INSERT INTO messages VALUES (?, '120363000000000001@g.us', ?, ?, '2026-03-02 09:30:00+00:00', 0, '')`,
			"B"+string(rune('0'+i)), sender, "row")
	}
	old.Close()
	session := filepath.Join(dir, "whatsapp.db")
	sess, _ := sql.Open("sqlite", session)
	sess.Exec(`CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)`)
	sess.Exec(`INSERT INTO whatsmeow_lid_map VALUES ('99990000000123', '15550100004')`)
	sess.Close()

	db := openTemp(t)
	st, err := db.ImportLegacy(ctx, src, LegacyOptions{SessionDB: session, Batch: 2})
	if err != nil || st.Messages != 7 || st.Skipped != 0 {
		t.Fatalf("stats %+v %v", st, err)
	}
	lid, _ := db.GetMessage(ctx, "120363000000000001@g.us", "B1")
	pn, _ := db.GetMessage(ctx, "120363000000000001@g.us", "B0")
	if lid.Author != "99990000000123@lid" || pn.Author != "15550100003@s.whatsapp.net" {
		t.Fatalf("authors: %q %q", lid.Author, pn.Author)
	}
	// Second run inserts nothing and counts every row as skipped.
	st, err = db.ImportLegacy(ctx, src, LegacyOptions{SessionDB: session, Batch: 2})
	if err != nil || st.Messages != 0 || st.Skipped != 7 {
		t.Fatalf("second run %+v %v", st, err)
	}
}

func TestLegacyTimeFormats(t *testing.T) {
	const want = 1767323045
	inputs := []any{
		"2026-01-02 03:04:05+00:00",
		"2026-01-02 03:04:05.5 +0000 UTC",
		"2026-01-02 03:04:05 +0000 UTC m=+12.5",
		"2026-01-02T03:04:05Z",
		int64(1767323045),
		int64(1767323045000),
		"1767323045",
		time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC),
		[]byte("2026-01-02 03:04:05.999999999-00:00"),
	}
	for _, in := range inputs {
		got, ok := legacyTime(in)
		if !ok || got != want {
			t.Errorf("%v: got %d ok=%v, want %d", in, got, ok, want)
		}
	}
	if _, ok := legacyTime("garbage"); ok {
		t.Error("garbage parsed")
	}
}

func TestLegacyLIDReadReportsInvalidRows(t *testing.T) {
	source := makeLegacyDB(t, false)
	path := filepath.Join(t.TempDir(), "ids.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`CREATE TABLE whatsmeow_lid_map (lid TEXT); INSERT INTO whatsmeow_lid_map VALUES (NULL)`); err != nil {
		t.Fatal(err)
	}
	if _, err := openTemp(t).ImportLegacy(ctx, source, LegacyOptions{SessionDB: path}); err == nil {
		t.Fatal("invalid LID row accepted")
	}
}
