package wa

import (
	"testing"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestNetworkCannotTouchImportedMessages(t *testing.T) {
	in, _ := newIngest(t)
	imp := store.ImportedPrefix + "0123456789abcdef01234567"
	in.DB.PutMessage(bg, &store.Message{Conv: bob.String(), ID: imp, Author: bob.String(), SentAt: 100, Body: "imported text"})
	// A message that claims an imported id is not stored.
	if m, err := in.Message(bg, msgEvent(bob, bob, imp, false, &waE2E.Message{Conversation: proto.String("overwritten")}), true); err != nil || m != nil {
		t.Fatalf("stored %+v %v", m, err)
	}
	// Nor can an edit, a revoke or a reaction target it.
	edit := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(),
		Key: &waCommon.MessageKey{ID: proto.String(imp)}, EditedMessage: &waE2E.Message{Conversation: proto.String("edited")}}}
	in.Message(bg, msgEvent(bob, bob, "E1", false, edit), true)
	revoke := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(),
		Key: &waCommon.MessageKey{ID: proto.String(imp)}}}
	in.Message(bg, msgEvent(bob, bob, "R1", false, revoke), true)
	react := &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Key: &waCommon.MessageKey{ID: proto.String(imp)}, Text: proto.String("ok")}}
	in.Message(bg, msgEvent(bob, bob, "X1", false, react), true)
	m, err := in.DB.GetMessage(bg, bob.String(), imp)
	if err != nil || m.Body != "imported text" || m.EditedAt != 0 || m.DeletedAt != 0 {
		t.Fatalf("imported message changed: %+v %v", m, err)
	}
	if r, _ := in.DB.ReactionsFor(bg, bob.String(), []string{imp}); len(r[imp]) != 0 {
		t.Fatalf("reaction stored: %v", r)
	}
	// Imported messages are never history anchors.
	if _, err := in.DB.OldestMessage(bg, bob.String()); err != store.ErrNotFound {
		t.Fatalf("anchor: %v", err)
	}
	in.DB.PutMessage(bg, &store.Message{Conv: bob.String(), ID: "WA1", Author: bob.String(), SentAt: 200, Body: "from the phone"})
	if a, err := in.DB.OldestMessage(bg, bob.String()); err != nil || a.ID != "WA1" {
		t.Fatalf("anchor: %+v %v", a, err)
	}
}
