package store

import (
	"context"
	"path/filepath"
	"testing"
)

func openImportedDB(t *testing.T) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func TestConversationsByTitle(t *testing.T) {
	db := openImportedDB(t)
	ctx := context.Background()
	for _, c := range []Conversation{
		{JID: "15550100002@s.whatsapp.net", Title: "Alice Example"},
		{JID: "15550100003@s.whatsapp.net", Title: "alice example"},
		{JID: "15550100004@s.whatsapp.net", Title: "Alice"},
	} {
		if err := db.TouchConversation(ctx, c); err != nil {
			t.Fatal(err)
		}
	}
	list, err := db.ConversationsByTitle(ctx, "ALICE EXAMPLE")
	if err != nil || len(list) != 2 {
		t.Fatalf("%v %v", list, err)
	}
	if list, err = db.ConversationsByTitle(ctx, "  "); err != nil || len(list) != 0 {
		t.Fatalf("empty title: %v %v", list, err)
	}
}

func TestInsertAndScanMessages(t *testing.T) {
	db := openImportedDB(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	if err := db.PutMessage(ctx, &Message{Conv: conv, ID: "keep", SentAt: 100, Body: "original"}); err != nil {
		t.Fatal(err)
	}
	list := []*Message{
		{Conv: conv, ID: "keep", SentAt: 100, Body: "replacement"},
		{Conv: conv, ID: "imp-1", SentAt: 50, Body: "early"},
		{Conv: conv, ID: "imp-2", SentAt: 200, Body: "late"},
		{Conv: conv, ID: "imp-3", SentAt: 300, Body: "later"},
	}
	n, err := db.InsertMessages(ctx, list, 2)
	if err != nil || n != 3 {
		t.Fatalf("inserted %d: %v", n, err)
	}
	if m, _ := db.GetMessage(ctx, conv, "keep"); m.Body != "original" {
		t.Errorf("an existing row was changed: %q", m.Body)
	}
	var seen []string
	if err := db.ScanMessages(ctx, conv, 60, 250, func(m *Message) { seen = append(seen, m.ID) }); err != nil {
		t.Fatal(err)
	}
	if len(seen) != 2 || seen[0] != "keep" || seen[1] != "imp-2" {
		t.Errorf("scan %v", seen)
	}
}

func TestLinkImportedFile(t *testing.T) {
	db := openImportedDB(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	for _, m := range []*Message{
		{Conv: conv, ID: "imp-a", SentAt: 1, Kind: KindUnsupported, Body: "[image: a.jpg]"},
		{Conv: conv, ID: "imp-b", SentAt: 2, Kind: KindImage, Body: "", LocalPath: "/kept/b.jpg"},
		{Conv: conv, ID: "3EB0", SentAt: 3, Kind: KindUnsupported, Body: "not imported"},
	} {
		if err := db.PutMessage(ctx, m); err != nil {
			t.Fatal(err)
		}
	}
	link := func(id string) bool {
		ok, err := db.LinkImportedFile(ctx, &Message{Conv: conv, ID: id, Kind: KindImage, Body: "caption",
			AttachmentName: "a.jpg", Mime: "image/jpeg", ByteSize: 4, LocalPath: "/new/a.jpg"})
		if err != nil {
			t.Fatal(err)
		}
		return ok
	}
	if !link("imp-a") || link("imp-a") || link("imp-b") || link("3EB0") {
		t.Fatal("only an imported row without a file is linked, once")
	}
	m, _ := db.GetMessage(ctx, conv, "imp-a")
	if m.Kind != KindImage || m.Body != "caption" || m.LocalPath != "/new/a.jpg" || m.ByteSize != 4 {
		t.Errorf("linked row %+v", m)
	}
	if m, _ = db.GetMessage(ctx, conv, "imp-b"); m.LocalPath != "/kept/b.jpg" {
		t.Errorf("an existing file path was replaced: %+v", m)
	}
}
