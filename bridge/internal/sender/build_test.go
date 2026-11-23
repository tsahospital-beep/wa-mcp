package sender

import (
	"os"
	"path/filepath"
	"testing"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestMentionTags(t *testing.T) {
	js, err := ParseMentions([]string{"+1 555 010 0002", "15550100003@s.whatsapp.net", "15550100002"})
	if err != nil {
		t.Fatal(err)
	}
	if len(js) != 2 || js[0].String() != "15550100002@s.whatsapp.net" {
		t.Fatalf("parsed %v", js)
	}
	if got := WithMentionTags("hi all", js); got != "hi all @15550100002 @15550100003" {
		t.Fatalf("tags appended: %q", got)
	}
	if got := WithMentionTags("@15550100002 hi", js); got != "@15550100002 hi @15550100003" {
		t.Fatalf("existing tag kept once: %q", got)
	}
	if got := WithMentionTags("", js[:1]); got != "@15550100002" {
		t.Fatalf("empty text: %q", got)
	}
	if _, err := ParseMentions([]string{"not-a-number"}); err == nil {
		t.Fatal("bad mention accepted")
	}
}

func TestTextMessageForms(t *testing.T) {
	if m := TextMessage("plain", nil); m.GetConversation() != "plain" || m.ExtendedTextMessage != nil {
		t.Fatalf("plain text must use Conversation: %v", m)
	}
	ci := BuildContext(&Reply{ID: "Q1"}, nil)
	if m := TextMessage("reply", ci); m.GetExtendedTextMessage().GetContextInfo().GetStanzaID() != "Q1" {
		t.Fatalf("reply must use ExtendedTextMessage: %v", m)
	}
	if BuildContext(nil, nil) != nil || BuildContext(&Reply{}, nil) != nil {
		t.Fatal("empty context must be nil")
	}
}

func TestReplyInGroupWithMentions(t *testing.T) {
	r := newRig(t, nil)
	group := "120363000000000001@g.us"
	carol := "15550100003@s.whatsapp.net"
	r.db.PutMessage(bg, &store.Message{Conv: group, ID: "Q42", Author: carol, SentAt: 50, Body: "Who brings the snacks?"})

	res, err := r.s.Send(bg, Request{
		To: group, Text: "I will", ReplyTo: &Reply{ID: "Q42"},
		Mentions: []string{"15550100003"}, Human: noHuman(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.tr.sent) != 1 || r.tr.sent[0].to.Server != types.GroupServer {
		t.Fatalf("sent %v", r.tr.sent)
	}
	x := r.tr.sent[0].msg.GetExtendedTextMessage()
	if x == nil {
		t.Fatalf("want ExtendedTextMessage, got %v", r.tr.sent[0].msg)
	}
	ci := x.GetContextInfo()
	if x.GetText() != "I will @15550100003" {
		t.Fatalf("text %q", x.GetText())
	}
	if ci.GetStanzaID() != "Q42" || ci.GetParticipant() != carol {
		t.Fatalf("reply target: id=%q participant=%q", ci.GetStanzaID(), ci.GetParticipant())
	}
	if ci.GetQuotedMessage().GetConversation() != "Who brings the snacks?" {
		t.Fatalf("quoted preview %q", ci.GetQuotedMessage().GetConversation())
	}
	if len(ci.GetMentionedJID()) != 1 || ci.GetMentionedJID()[0] != carol {
		t.Fatalf("mentioned %v", ci.GetMentionedJID())
	}

	stored, _ := r.db.GetMessage(bg, group, res.ID)
	if stored.ReplyToID != "Q42" || stored.ReplyToAuthor != carol || len(stored.Mentioned) != 1 {
		t.Fatalf("stored reply context: %+v", stored)
	}
}

func TestReplyToOwnMessage(t *testing.T) {
	r := newRig(t, nil)
	r.db.PutMessage(bg, &store.Message{Conv: bob, ID: "MINE", Outgoing: true, SentAt: 5, Kind: "image"})
	if _, err := r.s.Send(bg, Request{To: bob, Text: "about this photo", ReplyTo: &Reply{ID: "MINE"}, Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	ci := r.tr.sent[0].msg.GetExtendedTextMessage().GetContextInfo()
	if ci.GetParticipant() != "15550100001@s.whatsapp.net" || ci.GetQuotedMessage().GetConversation() != "[image]" {
		t.Fatalf("own reply: participant=%q preview=%q", ci.GetParticipant(), ci.GetQuotedMessage().GetConversation())
	}
}

func TestCaptionedMediaCarriesContext(t *testing.T) {
	r := newRig(t, nil)
	dir := t.TempDir()
	img := filepath.Join(dir, "cover.jpg")
	doc := filepath.Join(dir, "minutes.pdf")
	sticker := filepath.Join(dir, "wave.webp")
	for _, p := range []string{img, doc, sticker} {
		os.WriteFile(p, []byte("file "+p), 0o600)
	}

	if _, err := r.s.Send(bg, Request{To: bob, Text: "the cover", MediaPath: img, Mentions: []string{"15550100003"},
		ReplyTo: &Reply{ID: "X", Author: "15550100003", Preview: "send it"}, Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	im := r.tr.sent[0].msg.GetImageMessage()
	if im == nil || im.GetCaption() != "the cover @15550100003" || im.GetMimetype() != "image/jpeg" {
		t.Fatalf("image: %v", r.tr.sent[0].msg)
	}
	if ci := im.GetContextInfo(); ci.GetStanzaID() != "X" || ci.GetParticipant() != "15550100003@s.whatsapp.net" ||
		ci.GetQuotedMessage().GetConversation() != "send it" || len(ci.GetMentionedJID()) != 1 {
		t.Fatalf("image context: %v", ci)
	}

	if _, err := r.s.Send(bg, Request{To: bob, Text: "notes", MediaPath: doc, StickerPath: sticker, Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	d := r.tr.sent[1].msg.GetDocumentMessage()
	if d == nil || d.GetFileName() != "minutes.pdf" || d.GetTitle() != "minutes.pdf" || d.GetCaption() != "notes" || d.GetMimetype() != "application/pdf" {
		t.Fatalf("document: %v", r.tr.sent[1].msg)
	}
	st := r.tr.sent[2].msg.GetStickerMessage()
	if st == nil || st.GetMimetype() != "image/webp" || st.GetContextInfo() != nil {
		t.Fatalf("sticker: %v", r.tr.sent[2].msg)
	}
}

func openTemp(t *testing.T, name string) *os.File {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	os.WriteFile(p, []byte("x"), 0o600)
	f, err := os.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return f
}

func TestPlanRules(t *testing.T) {
	if _, err := plan("", nil, false, nil); err != ErrEmpty {
		t.Fatalf("empty: %v", err)
	}
	if _, err := plan("", nil, false, openTemp(t, "x.png")); err != ErrStickerFormat {
		t.Fatalf("non-webp sticker: %v", err)
	}
	if _, err := plan("", openTemp(t, "a.mp3"), true, nil); err != ErrNotOpus {
		t.Fatalf("mp3 as voice: %v", err)
	}
	parts, _ := plan("hi", openTemp(t, "a.mp3"), false, nil)
	if len(parts) != 2 || parts[0].kind != store.KindText || parts[1].kind != store.KindAudio || parts[1].ctx {
		t.Fatalf("text + audio: %+v", parts)
	}
	parts, _ = plan("", nil, false, openTemp(t, "s.webp"))
	if len(parts) != 1 || parts[0].kind != store.KindSticker || !parts[0].ctx {
		t.Fatalf("sticker only: %+v", parts)
	}
	parts, _ = plan("look", openTemp(t, "clip.MP4"), false, nil)
	if len(parts) != 1 || parts[0].kind != store.KindVideo || parts[0].text != "look" {
		t.Fatalf("video: %+v", parts)
	}
	// The same rules decide what a scheduled entry becomes.
	if k, _, _ := KindOf("/x/a.webp", false, ""); k != store.KindImage {
		t.Fatalf("webp media is an image: %s", k)
	}
	if k, _, _ := KindOf("", false, "/x/a.webp"); k != store.KindSticker {
		t.Fatalf("webp sticker: %s", k)
	}
	if _, _, err := KindOf("/x/a.mp3", true, ""); err != ErrNotOpus {
		t.Fatalf("mp3 voice: %v", err)
	}
}
