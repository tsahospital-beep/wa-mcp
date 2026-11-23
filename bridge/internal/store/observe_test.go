package store

import (
	"context"
	"path/filepath"
	"testing"
)

type seen struct {
	origin, id, body, title string
	deleted, edited         bool
	chat                    bool
}

func TestObserverSeesCommittedRows(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	var msgs []seen
	var chats []Conversation
	db.SetObserver(&Observer{
		Message: func(ctx context.Context, origin string, m *Message, c *Conversation) {
			s := seen{origin: origin, id: m.ID, body: m.Body, deleted: m.DeletedAt != 0, edited: m.EditedAt != 0}
			if c != nil {
				s.chat, s.title = true, c.Title
			}
			msgs = append(msgs, s)
		},
		Conversation: func(ctx context.Context, c *Conversation) { chats = append(chats, *c) },
	})
	conv := "15550100002@s.whatsapp.net"
	m := &Message{Conv: conv, ID: "M1", Author: conv, SentAt: 100, Body: "hello", Kind: KindText}
	if err := db.PutMessage(WithOrigin(ctx, OriginLive), m); err != nil {
		t.Fatal(err)
	}
	if err := db.TouchConversation(ctx, Conversation{JID: conv, Title: "Bob", LastActivity: 100}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.EditBody(ctx, conv, "M1", "hello again", 120); err != nil {
		t.Fatal(err)
	}
	if _, err := db.MarkDeleted(ctx, conv, "M1", conv, 130); err != nil {
		t.Fatal(err)
	}
	// No change, no report.
	if _, err := db.MarkDeleted(ctx, conv, "M1", conv, 140); err != nil {
		t.Fatal(err)
	}
	if _, err := db.SetTitle(ctx, conv, "Bob B", true); err != nil {
		t.Fatal(err)
	}

	want := []seen{
		{origin: OriginLive, id: "M1", body: "hello"},
		{origin: OriginEdit, id: "M1", body: "hello again", edited: true, chat: true, title: "Bob"},
		{origin: OriginDelete, id: "M1", body: "hello again", edited: true, deleted: true, chat: true, title: "Bob"},
	}
	if len(msgs) != len(want) {
		t.Fatalf("messages: %+v", msgs)
	}
	for i := range want {
		if msgs[i] != want[i] {
			t.Errorf("message %d: got %+v, want %+v", i, msgs[i], want[i])
		}
	}
	if len(chats) != 2 || chats[0].Title != "Bob" || chats[0].LastActivity != 100 || chats[1].Title != "Bob B" {
		t.Fatalf("chats: %+v", chats)
	}
	if OriginOf(ctx) != "" || OriginOf(WithOrigin(ctx, OriginSent)) != OriginSent {
		t.Fatal("origin context")
	}

	// The legacy import writes in a transaction and reports nothing.
	db.SetObserver(nil)
	if err := db.PutMessage(ctx, &Message{Conv: conv, ID: "M2", SentAt: 1, Body: "x"}); err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 3 {
		t.Fatal("removed observer still called")
	}
}
