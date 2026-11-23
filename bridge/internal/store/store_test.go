package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

var ctx = context.Background()

func openTemp(t *testing.T) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func TestMigrationsFresh(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	v, err := db.SchemaVersion(ctx)
	if err != nil || v != LatestSchema {
		t.Fatalf("schema version %d (%v), want %d", v, err, LatestSchema)
	}
	for _, table := range []string{"meta", "conversations", "chat_messages", "derived_text", "call_log", "scheduled_messages", "outbox_log"} {
		var n int
		if err := db.SQL().QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?`, table).Scan(&n); err != nil || n != 1 {
			t.Fatalf("table %s missing", table)
		}
	}
	db.Close()

	// Reopening is a no-op.
	db, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if v, _ := db.SchemaVersion(ctx); v != LatestSchema {
		t.Fatalf("version changed on reopen: %d", v)
	}
}

func TestMigrationFromVersion1(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(migrations[0]); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(`INSERT INTO meta (key, value) VALUES ('schema_version', '1')`); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(`INSERT INTO scheduled_messages (target, body, next_run, created_at) VALUES ('15550100001@s.whatsapp.net', 'hi', 100, 1)`); err != nil {
		t.Fatal(err)
	}
	raw.Close()

	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if v, _ := db.SchemaVersion(ctx); v != LatestSchema {
		t.Fatalf("not upgraded: %d", v)
	}
	list, err := db.ListScheduled(ctx, "")
	if err != nil || len(list) != 1 || list[0].Body != "hi" || list[0].AnchorDay != 0 {
		t.Fatalf("old row after upgrade: %+v %v", list, err)
	}
}

func TestNewerSchemaRefused(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	db.SetMeta(ctx, "schema_version", "999")
	db.Close()
	if _, err := Open(path); err == nil || !strings.Contains(err.Error(), "newer") {
		t.Fatalf("expected newer-schema error, got %v", err)
	}
}

func TestMessagesUpsertAndQuery(t *testing.T) {
	db := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	msgs := []Message{
		{Conv: conv, ID: "A1", Author: conv, SentAt: 1000, Body: "Hello Alice"},
		{Conv: conv, ID: "A2", Author: "15550100001@s.whatsapp.net", Outgoing: true, SentAt: 1010, Body: "Hi Bob"},
		{Conv: conv, ID: "A3", Author: conv, SentAt: 1020, Kind: "image", Body: "a photo of the book",
			Mime: "image/jpeg", DirectPath: "/v/t62/x.enc?oh=1", MediaKey: []byte{1, 2, 3}, ReplyToID: "A2",
			ReplyToAuthor: "15550100001@s.whatsapp.net", ReplyPreview: "Hi Bob", Mentioned: []string{"15550100001@s.whatsapp.net"}},
		{Conv: "120363000000000001@g.us", ID: "G1", Author: conv, SentAt: 1030, Body: "Book club tonight"},
	}
	for i := range msgs {
		if err := db.PutMessage(ctx, &msgs[i]); err != nil {
			t.Fatal(err)
		}
	}

	got, err := db.GetMessage(ctx, conv, "A3")
	if err != nil {
		t.Fatal(err)
	}
	if got.Kind != "image" || got.ReplyToID != "A2" || got.ReplyPreview != "Hi Bob" || len(got.Mentioned) != 1 ||
		string(got.MediaKey) != "\x01\x02\x03" || got.DirectPath != "/v/t62/x.enc?oh=1" {
		t.Fatalf("round trip mismatch: %+v", got)
	}

	// A later update without media fields keeps them; local_path is never overwritten.
	if err := db.SetLocalPath(ctx, conv, "A3", "/tmp/a.jpg"); err != nil {
		t.Fatal(err)
	}
	if err := db.PutMessage(ctx, &Message{Conv: conv, ID: "A3", Author: conv, SentAt: 1020, Kind: "image", LocalPath: "/other"}); err != nil {
		t.Fatal(err)
	}
	got, _ = db.GetMessage(ctx, conv, "A3")
	if got.Body != "a photo of the book" || got.LocalPath != "/tmp/a.jpg" || got.DirectPath == "" {
		t.Fatalf("upsert lost data: %+v", got)
	}

	if _, err := db.GetMessage(ctx, conv, "missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}

	list, _ := db.ListMessages(ctx, MessageQuery{Conv: conv})
	if len(list) != 3 || list[0].ID != "A3" {
		t.Fatalf("newest first: %+v", list)
	}
	list, _ = db.ListMessages(ctx, MessageQuery{Conv: conv, Ascending: true, Limit: 2})
	if len(list) != 2 || list[0].ID != "A1" {
		t.Fatalf("ascending+limit: %+v", list)
	}
	list, _ = db.ListMessages(ctx, MessageQuery{After: 1005, Before: 1030})
	if len(list) != 2 {
		t.Fatalf("after/before: %+v", list)
	}
	list, _ = db.ListMessages(ctx, MessageQuery{Query: "book"})
	if len(list) != 2 {
		t.Fatalf("text search: %+v", list)
	}
	list, _ = db.ListMessages(ctx, MessageQuery{Query: "100%"})
	if len(list) != 0 {
		t.Fatalf("LIKE wildcards must be escaped: %+v", list)
	}

	oldest, err := db.OldestMessage(ctx, conv)
	if err != nil || oldest.ID != "A1" {
		t.Fatalf("oldest: %+v %v", oldest, err)
	}

	if ok, err := db.EditBody(ctx, conv, "A1", "Hello Alice (edited)", 2000); err != nil || !ok {
		t.Fatal(err)
	}
	got, _ = db.GetMessage(ctx, conv, "A1")
	if got.Body != "Hello Alice (edited)" || got.EditedAt != 2000 {
		t.Fatalf("edit: %+v", got)
	}
}

func TestEditsSurviveHistoryResync(t *testing.T) {
	db := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &Message{Conv: conv, ID: "E1", Author: conv, SentAt: 10, Body: "first version"})
	db.EditBody(ctx, conv, "E1", "second version", 50)
	// An older edit arriving late does not win.
	if ok, _ := db.EditBody(ctx, conv, "E1", "stale edit", 40); ok {
		t.Fatal("older edit applied")
	}
	// A history re-sync brings the original text again.
	db.PutMessage(ctx, &Message{Conv: conv, ID: "E1", Author: conv, SentAt: 10, Body: "first version"})
	m, _ := db.GetMessage(ctx, conv, "E1")
	if m.Body != "second version" || m.EditedAt != 50 {
		t.Fatalf("history overwrote the edit: %+v", m)
	}
}

func TestDeletedMessagesHideText(t *testing.T) {
	db := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &Message{Conv: conv, ID: "D1", Author: conv, SentAt: 10, Body: "oops, wrong chat"})
	if ok, _ := db.MarkDeleted(ctx, conv, "D1", conv, 20); !ok {
		t.Fatal("not marked")
	}
	m, _ := db.GetMessage(ctx, conv, "D1")
	if m.Body != "" || m.DeletedAt != 20 {
		t.Fatalf("deleted text visible: %+v", m)
	}
	if list, _ := db.ListMessages(ctx, MessageQuery{Conv: conv}); len(list) != 1 || list[0].Body != "" {
		t.Fatalf("list shows text: %+v", list)
	}
	if list, _ := db.ListMessages(ctx, MessageQuery{Query: "wrong chat"}); len(list) != 0 {
		t.Fatalf("search finds deleted text: %+v", list)
	}
	list, _ := db.ListMessages(ctx, MessageQuery{Conv: conv, IncludeDeleted: true})
	if len(list) != 1 || list[0].Body != "oops, wrong chat" {
		t.Fatalf("include_deleted: %+v", list)
	}
	// A re-sync keeps the tombstone.
	db.PutMessage(ctx, &Message{Conv: conv, ID: "D1", Author: conv, SentAt: 10, Body: "oops, wrong chat"})
	if m, _ := db.GetMessage(ctx, conv, "D1"); m.DeletedAt != 20 {
		t.Fatalf("tombstone lost: %+v", m)
	}
}

func TestPendingChangesAndReactions(t *testing.T) {
	db := openTemp(t)
	conv := "120363000000000001@g.us"
	db.AddPending(ctx, PendingChange{Conv: conv, ID: "P1", Kind: ChangeEdit, Actor: "a@s.whatsapp.net", Body: "v2", At: 20})
	db.AddPending(ctx, PendingChange{Conv: conv, ID: "P1", Kind: ChangeEdit, Actor: "a@s.whatsapp.net", Body: "v1", At: 10})
	db.AddPending(ctx, PendingChange{Conv: conv, ID: "P1", Kind: ChangeRevoke, Actor: "a@s.whatsapp.net", At: 30})
	list, err := db.TakePending(ctx, conv, "P1")
	if err != nil || len(list) != 2 || list[0].Body != "v2" || list[1].Kind != ChangeRevoke {
		t.Fatalf("pending: %+v %v", list, err)
	}
	if again, _ := db.TakePending(ctx, conv, "P1"); len(again) != 0 {
		t.Fatal("pending not removed")
	}

	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "M1", Reactor: "a@s.whatsapp.net", Emoji: "+", At: 10})
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "M1", Reactor: "b@s.whatsapp.net", Emoji: "ok", At: 11})
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "M1", Reactor: "a@s.whatsapp.net", Emoji: "x", At: 5}) // older, ignored
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "M1", Reactor: "b@s.whatsapp.net", Emoji: "", At: 12}) // removed
	all, _ := db.ReactionsFor(ctx, conv, []string{"M1"})
	r := all["M1"]
	if len(r) != 1 || r[0].Reactor != "a@s.whatsapp.net" || r[0].Emoji != "+" {
		t.Fatalf("reactions: %+v", r)
	}
}

func TestSetTitle(t *testing.T) {
	db := openTemp(t)
	jid := "120363000000000001@g.us"
	db.TouchConversation(ctx, Conversation{JID: jid, IsGroup: true, LastActivity: 1})
	if ok, _ := db.SetTitle(ctx, jid, "Book club", false); !ok {
		t.Fatal("empty title not filled")
	}
	if ok, _ := db.SetTitle(ctx, jid, "Push name", false); ok {
		t.Fatal("soft update replaced a title")
	}
	if ok, _ := db.SetTitle(ctx, jid, "Book club 2", true); !ok {
		t.Fatal("rename not applied")
	}
	if ok, _ := db.SetTitle(ctx, "15550100009@s.whatsapp.net", "Nobody", true); ok {
		t.Fatal("SetTitle created a chat")
	}
	c, _ := db.GetConversation(ctx, jid)
	if c.Title != "Book club 2" {
		t.Fatalf("title %q", c.Title)
	}
}

func TestUnicodeCaseInsensitiveSearch(t *testing.T) {
	for _, encrypted := range []bool{false, true} {
		db := openTemp(t)
		if encrypted {
			db.SetupEncryption(ctx, true, "pw")
		}
		conv := "15550100002@s.whatsapp.net"
		db.PutMessage(ctx, &Message{Conv: conv, ID: "G1", SentAt: 1, Body: "Greetings from ΑΘΗΝΑ"})
		db.PutMessage(ctx, &Message{Conv: conv, ID: "G2", SentAt: 2, Body: "Ein Brief ÜBER Äpfel"})
		db.PutMessage(ctx, &Message{Conv: conv, ID: "G3", SentAt: 3, Body: "50% off, under_score"})
		for q, want := range map[string]string{"αθηνα": "G1", "über äpfel": "G2", "50%": "G3", "R_S": "G3"} {
			list, err := db.ListMessages(ctx, MessageQuery{Query: q})
			if err != nil || len(list) != 1 || list[0].ID != want {
				t.Errorf("encrypted=%v query %q: %+v %v", encrypted, q, list, err)
			}
		}
	}
}

func TestUnsealRules(t *testing.T) {
	// Encryption off: a contact's text that looks sealed is returned as is.
	plain := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	plain.PutMessage(ctx, &Message{Conv: conv, ID: "W1", SentAt: 1, Body: "wae1:not really sealed"})
	m, err := plain.GetMessage(ctx, conv, "W1")
	if err != nil || m.Body != "wae1:not really sealed" {
		t.Fatalf("plain store unsealed a value: %+v %v", m, err)
	}

	// Encryption on: the same text round-trips because every write is sealed.
	enc := openTemp(t)
	enc.SetupEncryption(ctx, true, "pw")
	var notes []string
	enc.Debug = func(format string, args ...any) { notes = append(notes, format) }
	enc.PutMessage(ctx, &Message{Conv: conv, ID: "W1", SentAt: 1, Body: "wae1:not really sealed"})
	enc.PutMessage(ctx, &Message{Conv: conv, ID: "W2", SentAt: 2, Body: "fine"})
	if m, _ := enc.GetMessage(ctx, conv, "W1"); m.Body != "wae1:not really sealed" {
		t.Fatalf("round trip: %q", m.Body)
	}

	// A damaged field comes back raw; the query still succeeds and the log has no text.
	enc.SQL().Exec(`UPDATE chat_messages SET body = 'wae1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' WHERE msg_id = 'W2'`)
	list, err := enc.ListMessages(ctx, MessageQuery{Conv: conv})
	if err != nil || len(list) != 2 {
		t.Fatalf("query failed: %v", err)
	}
	if list[0].Body != "wae1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" {
		t.Fatalf("damaged field: %q", list[0].Body)
	}
	if len(notes) == 0 || strings.Contains(strings.Join(notes, " "), "fine") {
		t.Fatalf("debug notes: %v", notes)
	}
}

func TestOutboxKey(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, _ := Open(path)
	k1, err := db.OutboxKey(ctx)
	if err != nil || len(k1) != 32 {
		t.Fatalf("key %v %v", k1, err)
	}
	db.Close()
	db, _ = Open(path)
	k2, _ := db.OutboxKey(ctx)
	if string(k1) != string(k2) {
		t.Fatal("random outbox key must persist")
	}
	db.Close()

	other := openTemp(t)
	k3, _ := other.OutboxKey(ctx)
	if string(k3) == string(k1) {
		t.Fatal("keys must differ per store")
	}
	other.SetupEncryption(ctx, true, "pw")
	k4, _ := other.OutboxKey(ctx)
	if string(k4) == string(k3) {
		t.Fatal("with encryption the key comes from the store key")
	}
}

func TestAccountJID(t *testing.T) {
	db := openTemp(t)
	if v, err := db.AccountJID(ctx); err != nil || v != "" {
		t.Fatalf("empty store: %q %v", v, err)
	}
	db.SetAccountJID(ctx, "15550100001@s.whatsapp.net")
	if v, _ := db.AccountJID(ctx); v != "15550100001@s.whatsapp.net" {
		t.Fatalf("account %q", v)
	}
}

func TestUnreadAndMarkRead(t *testing.T) {
	db := openTemp(t)
	conv := "15550100003@s.whatsapp.net"
	for _, m := range []Message{
		{Conv: conv, ID: "old", Author: conv, SentAt: 100, Body: "before our reply"},
		{Conv: conv, ID: "mine", Outgoing: true, SentAt: 200, Body: "our reply"},
		{Conv: conv, ID: "n1", Author: conv, SentAt: 300, Body: "new 1"},
		{Conv: conv, ID: "n2", Author: conv, SentAt: 310, Body: "new 2"},
	} {
		m := m
		db.PutMessage(ctx, &m)
	}
	unread, err := db.UnreadIncoming(ctx, conv, 10)
	if err != nil || len(unread) != 2 || unread[0].ID != "n2" {
		t.Fatalf("unread: %+v %v", unread, err)
	}
	if err := db.MarkRead(ctx, conv, []string{"n1", "n2"}, 400); err != nil {
		t.Fatal(err)
	}
	unread, _ = db.UnreadIncoming(ctx, conv, 10)
	if len(unread) != 0 {
		t.Fatalf("still unread: %+v", unread)
	}
	m, _ := db.GetMessage(ctx, conv, "n1")
	if m.ReadAt != 400 {
		t.Fatalf("read_at = %d", m.ReadAt)
	}
	db.MarkRead(ctx, conv, []string{"n1"}, 999)
	m, _ = db.GetMessage(ctx, conv, "n1")
	if m.ReadAt != 400 {
		t.Fatalf("read_at must keep the first value, got %d", m.ReadAt)
	}
}

func TestConversations(t *testing.T) {
	db := openTemp(t)
	db.TouchConversation(ctx, Conversation{JID: "15550100002@s.whatsapp.net", Title: "Bob", LastActivity: 50})
	db.TouchConversation(ctx, Conversation{JID: "120363000000000001@g.us", Title: "Book club", IsGroup: true, LastActivity: 70})
	// Empty title and older time do not overwrite.
	db.TouchConversation(ctx, Conversation{JID: "15550100002@s.whatsapp.net", LastActivity: 10})

	c, err := db.GetConversation(ctx, "15550100002@s.whatsapp.net")
	if err != nil || c.Title != "Bob" || c.LastActivity != 50 {
		t.Fatalf("conversation: %+v %v", c, err)
	}
	list, _ := db.ListConversations(ctx, "", 10, 0)
	if len(list) != 2 || list[0].Title != "Book club" || !list[0].IsGroup {
		t.Fatalf("list: %+v", list)
	}
	list, _ = db.ListConversations(ctx, "bob", 10, 0)
	if len(list) != 1 {
		t.Fatalf("filter: %+v", list)
	}
}

func TestCallOutcomes(t *testing.T) {
	db := openTemp(t)
	peer := "15550100004@s.whatsapp.net"

	db.NoteCall(ctx, Call{ID: "c1", Peer: peer, StartedAt: 100})
	db.EndCall(ctx, "c1", 130)
	db.NoteCall(ctx, Call{ID: "c2", Peer: peer, StartedAt: 200, Video: true})
	db.SetCallOutcome(ctx, "c2", CallAnswered)
	db.SetCallOutcome(ctx, "c2", CallRejected) // answered stays answered
	db.EndCall(ctx, "c2", 400)
	db.NoteCall(ctx, Call{ID: "c3", Peer: peer, StartedAt: 500})
	db.SetCallOutcome(ctx, "c3", CallRejected)
	db.EndCall(ctx, "c3", 505)

	want := map[string]string{"c1": CallMissed, "c2": CallAnswered, "c3": CallRejected}
	for id, outcome := range want {
		c, err := db.GetCall(ctx, id)
		if err != nil || c.Outcome != outcome || c.EndedAt == 0 {
			t.Fatalf("%s: %+v %v", id, c, err)
		}
	}
	list, _ := db.ListCalls(ctx, 150, 10)
	if len(list) != 2 || list[0].ID != "c3" || !list[1].Video {
		t.Fatalf("list since: %+v", list)
	}
}

func TestScheduledLifecycle(t *testing.T) {
	db := openTemp(t)
	s := &Scheduled{Target: "15550100002@s.whatsapp.net", Body: "Reminder", NextRun: 1000, Recurrence: "daily",
		CreatedAt: 900, Mentions: []string{"15550100002@s.whatsapp.net"}}
	id, err := db.AddScheduled(ctx, s)
	if err != nil || id == 0 {
		t.Fatal(err)
	}
	due, _ := db.DueScheduled(ctx, 999, 10)
	if len(due) != 0 {
		t.Fatal("not due yet")
	}
	due, _ = db.DueScheduled(ctx, 1000, 10)
	if len(due) != 1 || due[0].Body != "Reminder" || len(due[0].Mentions) != 1 {
		t.Fatalf("due: %+v", due)
	}
	ok, _ := db.ClaimScheduled(ctx, id)
	again, _ := db.ClaimScheduled(ctx, id)
	if !ok || again {
		t.Fatal("claim must succeed once")
	}
	if sending, _ := db.ListScheduled(ctx, SchedSending); len(sending) != 1 {
		t.Fatalf("sending rows: %d", len(sending))
	}
	db.UpdateScheduled(ctx, id, ScheduleUpdate{Status: SchedFailed, NextRun: 1000, Attempts: 1, LastError: "x"})
	got, _ := db.GetScheduled(ctx, id)
	if got.Status != SchedFailed || got.LastError != "x" {
		t.Fatalf("status %s", got.Status)
	}

	no := false
	kid, _ := db.AddScheduled(ctx, &Scheduled{Target: "x@s.whatsapp.net", Body: "note", MediaPath: "/m/a.ogg", Kind: "voice",
		Human: &no, NextRun: 5, CreatedAt: 1})
	k, _ := db.GetScheduled(ctx, kid)
	if k.Kind != "voice" || k.Human == nil || *k.Human {
		t.Fatalf("kind/human not stored: %+v", k)
	}
	db.CancelScheduled(ctx, kid)

	id2, _ := db.AddScheduled(ctx, &Scheduled{Target: "x@s.whatsapp.net", Body: "b", NextRun: 5, CreatedAt: 1})
	if ok, _ := db.CancelScheduled(ctx, id2); !ok {
		t.Fatal("cancel pending failed")
	}
	if ok, _ := db.CancelScheduled(ctx, id2); ok {
		t.Fatal("cancel twice must report false")
	}
	pending, _ := db.ListScheduled(ctx, SchedPending)
	all, _ := db.ListScheduled(ctx, "")
	if len(pending) != 0 || len(all) != 3 {
		t.Fatalf("list: pending=%d all=%d", len(pending), len(all))
	}
}

func TestOutboxCounts(t *testing.T) {
	db := openTemp(t)
	now := int64(1_000_000)
	db.ReserveSent(ctx, "a", "h1", now-4000, 1)
	db.ReserveSent(ctx, "a", "h2", now-100, 1)
	db.ReserveSent(ctx, "b", "h1", now-50, 1)
	if n, _ := db.CountSent(ctx, "a", now-3600); n != 1 {
		t.Fatalf("per chat hour: %d", n)
	}
	if n, _ := db.CountSent(ctx, "", now-3600); n != 2 {
		t.Fatalf("global hour: %d", n)
	}
	if dup, _ := db.SentRecently(ctx, "b", "h1", now-600); !dup {
		t.Fatal("duplicate not found")
	}
	if dup, _ := db.SentRecently(ctx, "a", "h1", now-600); dup {
		t.Fatal("old row counted as duplicate")
	}
	// Reservations count at once and can be released.
	ids, err := db.ReserveSent(ctx, "d", "h3", now, 2)
	if err != nil || len(ids) != 2 {
		t.Fatalf("reserve: %v %v", ids, err)
	}
	if n, _ := db.CountSent(ctx, "d", now-10); n != 2 {
		t.Fatalf("reserved rows not counted: %d", n)
	}
	db.ReleaseSent(ctx, ids[1:])
	if n, _ := db.CountSent(ctx, "d", now-10); n != 1 {
		t.Fatalf("release: %d", n)
	}
	// Pruning drops rows older than two days.
	db.ReserveSent(ctx, "c", "h", now+3*86400, 1)
	if n, _ := db.CountSent(ctx, "", 0); n != 1 {
		t.Fatalf("prune: %d rows left", n)
	}
}

func TestEncryptionAtRest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.SetupEncryption(ctx, true, ""); !errors.Is(err, ErrNoPassphrase) {
		t.Fatalf("empty passphrase: %v", err)
	}
	if err := db.SetupEncryption(ctx, true, "pass-one"); err != nil {
		t.Fatal(err)
	}
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &Message{Conv: conv, ID: "S1", SentAt: 10, Body: "secret plans", AttachmentName: "plan.pdf",
		ReplyPreview: "earlier", MediaKey: []byte{9, 9}, Kind: "document"})
	db.AddScheduled(ctx, &Scheduled{Target: conv, Body: "scheduled secret", NextRun: 1, CreatedAt: 1})

	var body, name, preview string
	var key []byte
	db.SQL().QueryRow(`SELECT body, attachment_name, reply_preview, media_key FROM chat_messages`).Scan(&body, &name, &preview, &key)
	for _, v := range []string{body, name, preview, string(key)} {
		if !strings.HasPrefix(v, "wae2:") {
			t.Fatalf("column not sealed: %q", v)
		}
	}
	m, err := db.GetMessage(ctx, conv, "S1")
	if err != nil || m.Body != "secret plans" || m.AttachmentName != "plan.pdf" || string(m.MediaKey) != "\x09\x09" {
		t.Fatalf("decrypt: %+v %v", m, err)
	}
	found, _ := db.ListMessages(ctx, MessageQuery{Query: "PLANS"})
	if len(found) != 1 {
		t.Fatalf("search on sealed data: %d", len(found))
	}
	db.Close()

	db, _ = Open(path)
	defer db.Close()
	if err := db.SetupEncryption(ctx, true, "pass-two"); !errors.Is(err, ErrWrongPassphrase) {
		t.Fatalf("wrong passphrase: %v", err)
	}
	if err := db.SetupEncryption(ctx, false, ""); !errors.Is(err, ErrEncryptedStore) {
		t.Fatalf("encrypted store without encryption: %v", err)
	}
	if err := db.SetupEncryption(ctx, true, "pass-one"); err != nil {
		t.Fatal(err)
	}
	s, _ := db.ListScheduled(ctx, "")
	if len(s) != 1 || s[0].Body != "scheduled secret" {
		t.Fatalf("scheduled decrypt: %+v", s)
	}
}

func TestSameIDKeepsAuthorAndDirection(t *testing.T) {
	db := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &Message{Conv: conv, ID: "O1", Author: "15550100001@s.whatsapp.net", Outgoing: true, SentAt: 10, Body: "I agree to pay 10"})
	db.PutMessage(ctx, &Message{Conv: conv, ID: "O1", Author: conv, Outgoing: false, SentAt: 99, Body: "same author re-sync", Mime: "text/plain"})
	m, _ := db.GetMessage(ctx, conv, "O1")
	if m.Author != "15550100001@s.whatsapp.net" || !m.Outgoing || m.SentAt != 10 {
		t.Fatalf("author/direction/time changed: %+v", m)
	}
	if m.Mime != "text/plain" {
		t.Fatalf("other fields must still update: %+v", m)
	}
}

func TestUnverifiedDeletions(t *testing.T) {
	db := openTemp(t)
	group := "120363000000000001@g.us"
	carol := "15550100003@s.whatsapp.net"
	db.PutMessage(ctx, &Message{Conv: group, ID: "U1", Author: "15550100002@s.whatsapp.net", SentAt: 1, Body: "task list"})
	db.MarkDeletedUnverified(ctx, group, "U1", carol, 5)
	m, _ := db.GetMessage(ctx, group, "U1")
	if m.Body != "task list" || m.DeletedUnverified != 5 || m.DeletedAt != 0 || m.DeletedBy != carol {
		t.Fatalf("unverified deletion must keep the text: %+v", m)
	}
	if n, _ := db.VerifyDeletions(ctx, group, []string{"15550100009@s.whatsapp.net"}); n != 0 {
		t.Fatal("verified by a non-admin")
	}
	if n, _ := db.VerifyDeletions(ctx, group, []string{carol}); n != 1 {
		t.Fatal("admin deletion not verified")
	}
	m, _ = db.GetMessage(ctx, group, "U1")
	if m.Body != "" || m.DeletedAt != 5 || m.DeletedUnverified != 0 {
		t.Fatalf("after verification: %+v", m)
	}
}

func TestPruningAndReactionCap(t *testing.T) {
	db := openTemp(t)
	now := time.Unix(1_800_000_000, 0)
	db.Now = func() time.Time { return now }
	conv := "15550100002@s.whatsapp.net"
	db.AddPending(ctx, PendingChange{Conv: conv, ID: "OLD", Kind: ChangeEdit, Body: "x", At: 1})
	now = now.Add(PendingWindow + time.Hour)
	db.AddPending(ctx, PendingChange{Conv: conv, ID: "NEW", Kind: ChangeEdit, Body: "y", At: 2})
	var n int
	db.SQL().QueryRow(`SELECT COUNT(*) FROM pending_changes`).Scan(&n)
	if n != 1 {
		t.Fatalf("old pending change not pruned: %d rows", n)
	}

	big := strings.Repeat("A", 1000)
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "GHOST", Reactor: "b@s.whatsapp.net", Emoji: big, At: 1})
	db.PutMessage(ctx, &Message{Conv: conv, ID: "REAL", SentAt: 1, Body: "hi"})
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "REAL", Reactor: "b@s.whatsapp.net", Emoji: "é" + strings.Repeat("é", 40), At: 1})
	r, _ := db.ReactionsFor(ctx, conv, []string{"GHOST", "REAL"})
	if len(r["GHOST"]) != 1 || len(r["GHOST"][0].Emoji) != MaxReactionText {
		t.Fatalf("cap: %+v", r)
	}
	if e := r["REAL"][0].Emoji; len(e) > MaxReactionText || !utf8.ValidString(e) {
		t.Fatalf("cap must keep valid UTF-8: %q", e)
	}
	// After the pending window the reaction to the unknown message goes; the other stays.
	now = now.Add(PendingWindow + time.Hour)
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "REAL", Reactor: "c@s.whatsapp.net", Emoji: "ok", At: 2})
	r, _ = db.ReactionsFor(ctx, conv, []string{"GHOST", "REAL"})
	if len(r["GHOST"]) != 0 || len(r["REAL"]) != 2 {
		t.Fatalf("orphan prune: %+v", r)
	}
}

func TestPendingCountCap(t *testing.T) {
	db := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	tx, _ := db.SQL().Begin()
	for i := 0; i < MaxPending+50; i++ {
		tx.Exec(`INSERT INTO pending_changes (conv, msg_id, kind, at, added_at) VALUES (?, ?, 'edit', 1, ?)`, conv, fmt.Sprint("P", i), time.Now().Unix())
	}
	tx.Commit()
	db.AddPending(ctx, PendingChange{Conv: conv, ID: "LAST", Kind: ChangeEdit, Body: "x", At: 2})
	var n int
	db.SQL().QueryRow(`SELECT COUNT(*) FROM pending_changes`).Scan(&n)
	if n != MaxPending {
		t.Fatalf("count cap: %d rows", n)
	}
}

func TestSealedSearchPagesAndSkipsDeleted(t *testing.T) {
	db := openTemp(t)
	db.SetupEncryption(ctx, true, "pw")
	old := searchPage
	searchPage = 2
	defer func() { searchPage = old }()
	conv := "15550100002@s.whatsapp.net"
	for i := 0; i < 7; i++ {
		body := "noise"
		if i%3 == 0 {
			body = "needle " + fmt.Sprint(i)
		}
		db.PutMessage(ctx, &Message{Conv: conv, ID: fmt.Sprint("S", i), SentAt: int64(100 + i), Body: body})
	}
	db.PutMessage(ctx, &Message{Conv: conv, ID: "DEL", SentAt: 200, Body: "needle deleted", AttachmentName: "needle.pdf", Kind: KindDocument})
	db.MarkDeleted(ctx, conv, "DEL", conv, 201)
	list, err := db.ListMessages(ctx, MessageQuery{Query: "NEEDLE"})
	if err != nil || len(list) != 3 {
		t.Fatalf("paged sealed search: %d %v", len(list), err)
	}
	list, _ = db.ListMessages(ctx, MessageQuery{Query: "needle", Offset: 1, Limit: 1})
	if len(list) != 1 || list[0].ID != "S3" {
		t.Fatalf("offset across pages: %+v", list)
	}
	list, _ = db.ListMessages(ctx, MessageQuery{Query: "needle", IncludeDeleted: true})
	if len(list) != 4 {
		t.Fatalf("include_deleted search: %d", len(list))
	}
}

type foldVectors struct {
	Fold []struct {
		Input  string `json:"input"`
		Folded string `json:"folded"`
	} `json:"fold"`
	Search []struct {
		Text  string `json:"text"`
		Query string `json:"query"`
		Match bool   `json:"match"`
	} `json:"search"`
}

// The same vectors are checked by the Python tests against str.casefold.
func TestSharedFoldVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "fold_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v foldVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	for _, f := range v.Fold {
		if got := Fold(f.Input); got != f.Folded {
			t.Errorf("Fold(%q) = %q, want %q", f.Input, got, f.Folded)
		}
	}
	for _, encrypted := range []bool{false, true} {
		db := openTemp(t)
		if encrypted {
			db.SetupEncryption(ctx, true, "pw")
		}
		for i, s := range v.Search {
			db.PutMessage(ctx, &Message{Conv: "c@s.whatsapp.net", ID: fmt.Sprint("V", i), SentAt: int64(i + 1), Body: s.Text})
		}
		for i, s := range v.Search {
			list, _ := db.ListMessages(ctx, MessageQuery{Query: s.Query, Limit: 100})
			found := false
			for _, m := range list {
				found = found || m.ID == fmt.Sprint("V", i)
			}
			if found != s.Match {
				t.Errorf("encrypted=%v search %q in %q: found=%v want %v", encrypted, s.Query, s.Text, found, s.Match)
			}
		}
	}
}
