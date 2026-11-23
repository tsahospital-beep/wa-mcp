package wabridge

import (
	"bytes"
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

var (
	bg    = context.Background()
	me    = types.NewJID("15550100001", types.DefaultUserServer)
	bob   = types.NewJID("15550100002", types.DefaultUserServer)
	carol = types.NewJID("15550100003", types.DefaultUserServer)
	club  = types.NewJID("120363000000000001", types.GroupServer)
)

func event(chat, from types.JID, id string, fromMe bool, at int64, m *waE2E.Message) *events.Message {
	return &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{Chat: chat, Sender: from, IsFromMe: fromMe, IsGroup: chat.Server == types.GroupServer},
			ID:            id, Timestamp: time.Unix(at, 0), PushName: "Bob",
		},
		Message: m,
	}
}

type fakeTransport struct{ n int }

func (f *fakeTransport) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	f.n++
	return "OUT" + string(rune('0'+f.n)), time.Unix(1_800_000_500, 0), nil
}
func (f *fakeTransport) Upload(ctx context.Context, r io.Reader, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
	data, _ := io.ReadAll(r)
	return whatsmeow.UploadResponse{URL: "https://mmg.whatsapp.net/x", DirectPath: "/x", FileLength: uint64(len(data)),
		FileSHA256: []byte{1, 2, 3, 4, 5, 6, 7, 8, 9}}, nil
}
func (f *fakeTransport) ChatPresence(ctx context.Context, to types.JID, s types.ChatPresence, m types.ChatPresenceMedia) error {
	return nil
}
func (f *fakeTransport) MarkRead(ctx context.Context, ids []string, at time.Time, chat, from types.JID) error {
	return nil
}
func (f *fakeTransport) OwnJID() types.JID { return me }

type ctxKey struct{}

// TestHooksSeeEveryWritePath drives the real ingest and sender and checks
// what OnMessageStored and OnChatStored receive.
func TestHooksSeeEveryWritePath(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var msgs []StoredMessage
	var tags []any
	var chats []StoredChat
	opts := Options{
		OnMessageStored: func(ctx context.Context, m StoredMessage) {
			msgs = append(msgs, m)
			tags = append(tags, ctx.Value(ctxKey{}))
		},
		OnChatStored: func(ctx context.Context, c StoredChat) { chats = append(chats, c) },
	}
	db.SetObserver(observer(opts, log.New(io.Discard, "", 0)))
	in := &wa.Ingest{
		DB:   db,
		Self: func() (types.JID, types.JID) { return me, types.JID{} },
		Title: func(ctx context.Context, chat types.JID, push string) string {
			if chat == club {
				return ""
			}
			return push
		},
	}

	// Live text, a captioned image, a sticker, a reply in a group.
	text := &waE2E.Message{Conversation: proto.String("hello")}
	img := &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("look"), Mimetype: proto.String("image/jpeg"),
		URL: proto.String("https://mmg.whatsapp.net/v/img.enc"), DirectPath: proto.String("/v/img.enc"),
		MediaKey: []byte("key"), FileSHA256: []byte("plain"), FileEncSHA256: []byte("enc"), FileLength: proto.Uint64(42)}}
	sticker := &waE2E.Message{StickerMessage: &waE2E.StickerMessage{FileSHA256: []byte{0xab, 0xcd, 1, 2, 3, 4, 5, 6, 7}}}
	reply := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("yes"),
		ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String("G0"), Participant: proto.String(carol.String()),
			QuotedMessage: &waE2E.Message{Conversation: proto.String("Which day works?")}}}}
	for _, e := range []*events.Message{
		event(bob, bob, "M1", false, 1_800_000_001, text),
		event(bob, bob, "M2", false, 1_800_000_002, img),
		event(bob, me, "M3", true, 1_800_000_003, sticker),
		event(club, carol, "G1", false, 1_800_000_004, reply),
	} {
		if _, err := in.Message(bg, e, true); err != nil {
			t.Fatal(err)
		}
	}
	// An edit and a deletion by the author.
	edit := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(),
		Key: &waCommon.MessageKey{ID: proto.String("M1")}, EditedMessage: &waE2E.Message{Conversation: proto.String("hello again")}}}
	revoke := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(),
		Key: &waCommon.MessageKey{ID: proto.String("M2")}}}
	in.Message(bg, event(bob, bob, "E1", false, 1_800_000_010, edit), true)
	in.Message(bg, event(bob, bob, "E2", false, 1_800_000_011, revoke), true)
	// A history message.
	hist := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{
		ID: proto.String(club.String()), Name: proto.String("Book club"),
		Messages: []*waHistorySync.HistorySyncMsg{{Message: &waWeb.WebMessageInfo{
			Key:              &waCommon.MessageKey{ID: proto.String("G0"), RemoteJID: proto.String(club.String()), Participant: proto.String(carol.String())},
			MessageTimestamp: proto.Uint64(1_799_999_000), Message: &waE2E.Message{Conversation: proto.String("Which day works?")},
		}}},
	}}}
	in.History(bg, hist, func(chat types.JID, w *waWeb.WebMessageInfo) (*events.Message, error) {
		from, _ := types.ParseJID(w.GetKey().GetParticipant())
		return event(chat, from, w.GetKey().GetID(), false, int64(w.GetMessageTimestamp()), w.GetMessage()), nil
	})
	// A send through the pipeline, with a value on the context.
	cfg := config.Default().Sending
	cfg.Enabled = true
	snd := &sender.Sender{Cfg: cfg, DB: db, Transport: &fakeTransport{}, Sleep: func(context.Context, time.Duration) error { return nil }}
	if _, err := snd.Send(context.WithValue(bg, ctxKey{}, "mine"), sender.Request{To: bob.String(), Text: "sent by us"}); err != nil {
		t.Fatal(err)
	}

	byID := map[string][]StoredMessage{}
	for _, m := range msgs {
		byID[m.ID] = append(byID[m.ID], m)
	}
	m1 := byID["M1"]
	if len(m1) != 2 || m1[0].Origin != OriginLive || m1[0].Body != "hello" || m1[0].Kind != KindText ||
		m1[0].Author != bob.String() || m1[0].SentAt.Unix() != 1_800_000_001 || m1[0].ChatTitle != "" {
		t.Fatalf("M1 live: %+v", m1)
	}
	if m1[1].Origin != OriginEdit || m1[1].Body != "hello again" || m1[1].EditedAt.IsZero() || m1[1].ChatTitle != "Bob" {
		t.Fatalf("M1 edit: %+v", m1[1])
	}
	m2 := byID["M2"]
	if len(m2) != 2 || m2[0].Kind != KindImage || m2[0].Body != "look" || m2[0].URL != "https://mmg.whatsapp.net/v/img.enc" ||
		m2[0].DirectPath != "/v/img.enc" || string(m2[0].MediaKey) != "key" || string(m2[0].FileSHA256) != "plain" ||
		string(m2[0].FileEncSHA256) != "enc" || m2[0].Size != 42 || m2[0].Mime != "image/jpeg" {
		t.Fatalf("M2: %+v", m2)
	}
	if m2[1].Origin != OriginDelete || m2[1].DeletedAt.IsZero() || m2[1].Body != "look" {
		t.Fatalf("M2 delete keeps the text for the hook: %+v", m2[1])
	}
	m3 := byID["M3"][0]
	if !m3.Outgoing || m3.Kind != KindSticker || m3.AttachmentName != "sticker_abcd010203040506.webp" || m3.Author != me.String() {
		t.Fatalf("M3: %+v", m3)
	}
	g1 := byID["G1"][0]
	if g1.ReplyToID != "G0" || g1.ReplyToAuthor != carol.String() || g1.ReplyPreview != "Which day works?" || g1.Author != carol.String() {
		t.Fatalf("G1: %+v", g1)
	}
	if g0 := byID["G0"]; len(g0) != 1 || g0[0].Origin != OriginHistory {
		t.Fatalf("G0: %+v", g0)
	}
	out := byID["OUT1"]
	if len(out) != 1 || out[0].Origin != OriginSent || !out[0].Outgoing || out[0].Body != "sent by us" {
		t.Fatalf("sent: %+v", out)
	}
	for i, m := range msgs {
		if m.ID == "OUT1" && tags[i] != "mine" {
			t.Fatalf("the send context did not reach the hook: %v", tags[i])
		}
	}
	titles := map[string]string{}
	for _, c := range chats {
		titles[c.JID] = c.Title
	}
	if titles[bob.String()] != "Bob" || titles[club.String()] != "Book club" {
		t.Fatalf("chats: %+v", chats)
	}
}

func TestHookPanicIsLogged(t *testing.T) {
	var buf bytes.Buffer
	o := observer(Options{OnChatStored: func(context.Context, StoredChat) { panic("boom") }}, log.New(&buf, "", 0))
	o.Conversation(bg, &store.Conversation{JID: bob.String()})
	if !strings.Contains(buf.String(), "OnChatStored hook: boom") {
		t.Fatalf("log: %q", buf.String())
	}
	if observer(Options{}, nil) != nil {
		t.Fatal("no hooks should mean no observer")
	}
}

type fakeDeps struct{ Deps }

func (fakeDeps) OwnJID() string { return me.String() }

func TestRoutesThroughTheAPIChecks(t *testing.T) {
	var gotDeps Deps
	routes := func(r Router, d Deps) error {
		gotDeps = d
		if err := r.HandleWithoutToken("/api/old", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Write([]byte("old " + gotDeps.OwnJID()))
		})); err != nil {
			return err
		}
		if err := r.Handle("GET /api/extra", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Write([]byte("extra"))
		})); err != nil {
			return err
		}
		if err := r.Handle("/api/v1/status", http.NotFoundHandler()); err == nil {
			t.Error("a /api/v1 route was accepted")
		}
		if err := r.HandleWithoutToken("/api/old", http.NotFoundHandler()); err == nil {
			t.Error("a duplicate route was accepted")
		}
		return nil
	}
	const token = "0123456789abcdef0123456789abcdef"
	srv := httptest.NewUnstartedServer(nil)
	h, err := RoutesHandler(srv.Listener.Addr().String(), token, fakeDeps{}, routes)
	if err != nil {
		t.Fatal(err)
	}
	srv.Config.Handler = h
	srv.Start()
	defer srv.Close()
	get := func(path, auth, host string) (int, string) {
		req, _ := http.NewRequest("GET", srv.URL+path, nil)
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		if host != "" {
			req.Host = host
		}
		resp, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}
	if code, body := get("/api/old", "", ""); code != 200 || body != "old "+me.String() {
		t.Fatalf("without token: %d %q", code, body)
	}
	if code, _ := get("/api/old", "", "evil.example:80"); code != 403 {
		t.Fatalf("Host check: %d", code)
	}
	if code, _ := get("/api/extra", "", ""); code != 401 {
		t.Fatalf("token route without token: %d", code)
	}
	if code, body := get("/api/extra", token, ""); code != 200 || body != "extra" {
		t.Fatalf("token route: %d %q", code, body)
	}
	if code, _ := get("/api/other", token, ""); code != 404 {
		t.Fatalf("unknown path: %d", code)
	}
	if _, err := RoutesHandler("127.0.0.1:1", token, nil, func(Router, Deps) error { return io.EOF }); err != io.EOF {
		t.Fatalf("Routes error not returned: %v", err)
	}
}

func TestConfigSectionsAndSessionFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{"legacy": {"port": 8080, "list": [{"a": 1}]}, "other": 1}`), 0o600)
	var legacy struct {
		Port int `json:"port"`
	}
	cfg, err := loadConfig(Options{ConfigPath: path, SessionFile: "whatsapp.db", ConfigSections: map[string]any{"legacy": &legacy}}, "")
	if err != nil {
		t.Fatal(err)
	}
	if legacy.Port != 8080 {
		t.Fatalf("section not decoded: %+v", legacy)
	}
	if len(cfg.Warnings) != 1 || !strings.Contains(cfg.Warnings[0], `"other"`) {
		t.Fatalf("warnings: %q", cfg.Warnings)
	}
	if cfg.SessionDB() != filepath.Join(dir, "store", "whatsapp.db") {
		t.Fatalf("session: %s", cfg.SessionDB())
	}
	if _, err := loadConfig(Options{ConfigPath: path, SessionFile: "../x.db"}, ""); err == nil {
		t.Fatal("bad session file accepted")
	}
	os.WriteFile(path, []byte(`{"legacy": {"port": "x"}}`), 0o600)
	if _, err := loadConfig(Options{ConfigPath: path, ConfigSections: map[string]any{"legacy": &legacy}}, ""); err == nil {
		t.Fatal("bad section accepted")
	}
}
