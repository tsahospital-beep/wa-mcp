package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

const token = "test-token-0123456789abcdef0123456789"

type fakeBridge struct {
	reads   [][]string
	history []string
	readErr error
	link    string
}

func (f *fakeBridge) LoginURL() (string, error) {
	if f.link == "" {
		return "", wa.ErrNoLogin
	}
	return f.link, nil
}

func (f *fakeBridge) Connected() bool { return true }
func (f *fakeBridge) LoggedIn() bool  { return true }
func (f *fakeBridge) Account() string { return "+1 ••• ••• 0101" }
func (f *fakeBridge) MarkRead(ctx context.Context, ids []string, at time.Time, chat, sender types.JID) error {
	if f.readErr != nil {
		return f.readErr
	}
	f.reads = append(f.reads, append([]string{chat.String(), sender.String()}, ids...))
	return nil
}
func (f *fakeBridge) RequestHistory(ctx context.Context, anchor *store.Message, count int) error {
	f.history = append(f.history, anchor.ID)
	return nil
}

type fakeTransport struct{ sent []*waE2E.Message }

func (f *fakeTransport) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	f.sent = append(f.sent, msg)
	return "OUT1", time.Unix(1700000000, 0), nil
}
func (f *fakeTransport) Upload(ctx context.Context, r io.Reader, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
	data, _ := io.ReadAll(r)
	return whatsmeow.UploadResponse{URL: "https://mmg.whatsapp.net/x", DirectPath: "/x", FileLength: uint64(len(data))}, nil
}
func (f *fakeTransport) ChatPresence(ctx context.Context, to types.JID, s types.ChatPresence, m types.ChatPresenceMedia) error {
	return nil
}
func (f *fakeTransport) MarkRead(ctx context.Context, ids []string, at time.Time, chat, sender types.JID) error {
	return nil
}
func (f *fakeTransport) OwnJID() types.JID {
	return types.NewJID("15550100001", types.DefaultUserServer)
}

type fakeMedia struct{}

func (fakeMedia) Fetch(ctx context.Context, conv, id string) (*media.Result, error) {
	if id == "IMG" {
		return &media.Result{Path: "/downloads/x/image_IMG.jpg", Kind: "image"}, nil
	}
	return nil, store.ErrNotFound
}

type env struct {
	srv    *httptest.Server
	cfg    *config.Config
	db     *store.DB
	bridge *fakeBridge
	tr     *fakeTransport
	snd    *sender.Sender
}

func newEnv(t *testing.T, sending bool) *env {
	t.Helper()
	dir := t.TempDir()
	cfg := config.Default()
	cfg.Dir = dir
	cfg.StoreDir = filepath.Join(dir, "store")
	cfg.Sending.Enabled = sending
	cfg.Sending.AllowedMediaDirs = []string{filepath.Join(dir, "outbox")}
	os.MkdirAll(cfg.Sending.AllowedMediaDirs[0], 0o755)
	cfg.Sending.DeniedPaths = []string{cfg.StoreDir}
	db, err := store.Open(cfg.MessagesDB())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	e := &env{cfg: cfg, db: db, bridge: &fakeBridge{}, tr: &fakeTransport{}}
	e.snd = &sender.Sender{Cfg: cfg.Sending, DB: db, Transport: e.tr, Sleep: func(context.Context, time.Duration) error { return nil }}
	s := &Server{
		Cfg: cfg, DB: db, WA: e.bridge, Send: e.snd.Send, Media: fakeMedia{}, Token: token, Version: "test",
		Avatar: func(ctx context.Context, jid types.JID) (string, error) { return "", media.ErrNoAvatar },
	}
	e.srv = httptest.NewUnstartedServer(nil)
	e.srv.Config.Handler = s.Handler(e.srv.Listener.Addr().String())
	e.srv.Start()
	t.Cleanup(e.srv.Close)
	return e
}

func (e *env) do(t *testing.T, method, path string, body any, mutate ...func(*http.Request)) (int, map[string]any) {
	t.Helper()
	var raw []byte
	switch b := body.(type) {
	case nil:
	case string:
		raw = []byte(b)
	default:
		raw, _ = json.Marshal(b)
	}
	var resp *http.Response
	var err error
	// A busy machine can drop a loopback connect; retry the dial a few times.
	for attempt := 0; attempt < 3; attempt++ {
		req, _ := http.NewRequest(method, e.srv.URL+path, bytes.NewReader(raw))
		req.Header.Set("Authorization", "Bearer "+token)
		for _, m := range mutate {
			m(req)
		}
		if resp, err = e.srv.Client().Do(req); err == nil || !strings.Contains(err.Error(), "dial tcp") {
			break
		}
	}
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestAuthAndHost(t *testing.T) {
	e := newEnv(t, false)
	code, out := e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Header.Del("Authorization") })
	if code != 401 || out["ok"] != false || out["error"] == "" {
		t.Fatalf("no token: %d %v", code, out)
	}
	code, _ = e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Header.Set("Authorization", "Bearer wrong") })
	if code != 401 {
		t.Fatalf("wrong token: %d", code)
	}
	code, _ = e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Header.Set("Authorization", token) })
	if code != 401 {
		t.Fatalf("token without Bearer: %d", code)
	}
	code, out = e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Host = "attacker.example:8080" })
	if code != 403 || out["error"] != "host not allowed" {
		t.Fatalf("wrong host: %d %v", code, out)
	}
	code, out = e.do(t, "GET", "/api/v1/status", nil)
	if code != 200 || out["ok"] != true || out["connected"] != true || out["version"] != "test" || out["sending_enabled"] != false {
		t.Fatalf("status: %d %v", code, out)
	}
	code, out = e.do(t, "GET", "/api/v1/nope", nil)
	if code != 404 || out["ok"] != false {
		t.Fatalf("unknown path: %d %v", code, out)
	}
	code, out = e.do(t, "DELETE", "/api/v1/status", nil)
	if code != 405 || out["ok"] != false {
		t.Fatalf("wrong method: %d %v", code, out)
	}
}

func TestLocalhostAliasAccepted(t *testing.T) {
	e := newEnv(t, false)
	addr := e.srv.Listener.Addr().String()
	port := addr[strings.LastIndex(addr, ":")+1:]
	code, _ := e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Host = "localhost:" + port })
	if code != 200 {
		t.Fatalf("localhost alias: %d", code)
	}
	code, _ = e.do(t, "GET", "/api/v1/status", nil, func(r *http.Request) { r.Host = "localhost:1" })
	if code != 403 {
		t.Fatalf("other port: %d", code)
	}
}

func TestSendingDisabled(t *testing.T) {
	e := newEnv(t, false)
	code, out := e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100002", "text": "hi"})
	if code != 403 || !strings.Contains(out["error"].(string), "sending.enabled") {
		t.Fatalf("send: %d %v", code, out)
	}
	code, _ = e.do(t, "POST", "/api/v1/schedule", map[string]any{"to": "15550100002", "text": "hi", "at": time.Now().Add(time.Hour).Format(time.RFC3339)})
	if code != 403 {
		t.Fatalf("schedule: %d", code)
	}
	if len(e.tr.sent) != 0 {
		t.Fatal("message went out")
	}
}

func TestSendFlow(t *testing.T) {
	e := newEnv(t, true)
	code, out := e.do(t, "POST", "/api/v1/send", map[string]any{"to": "+1 555 010 0002", "text": "See you", "human": false})
	if code != 200 || out["id"] != "OUT1" || out["sent_at"].(float64) != 1700000000 {
		t.Fatalf("send: %d %v", code, out)
	}
	if _, ok := out["waited_ms"]; !ok {
		t.Fatal("waited_ms missing")
	}
	code, out = e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100002", "text": "See you", "human": false})
	if code != 409 {
		t.Fatalf("duplicate: %d %v", code, out)
	}

	e.snd.Cfg.Limits.PerChatPerHour = 1
	code, _ = e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100002", "text": "another", "human": false})
	if code != 429 {
		t.Fatalf("limit: %d", code)
	}
	e.snd.Cfg.Limits.PerChatPerHour = 20

	outside := filepath.Join(t.TempDir(), "secret.pdf")
	os.WriteFile(outside, []byte("x"), 0o600)
	code, out = e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100003", "media_path": outside})
	if code != 403 || !strings.Contains(out["error"].(string), "allowed_media_dirs") {
		t.Fatalf("outside media dir: %d %v", code, out)
	}
	code, _ = e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100003", "media_path": filepath.Join(e.cfg.Sending.AllowedMediaDirs[0], "missing.jpg")})
	if code != 400 {
		t.Fatalf("missing media: %d", code)
	}
	code, _ = e.do(t, "POST", "/api/v1/send", map[string]any{"to": "nobody", "text": "x"})
	if code != 400 {
		t.Fatalf("bad target: %d", code)
	}
	code, _ = e.do(t, "POST", "/api/v1/send", `{"to": "15550100002", "text": `)
	if code != 400 {
		t.Fatalf("broken JSON: %d", code)
	}
	big := map[string]any{"to": "15550100002", "text": strings.Repeat("a", 300<<10)}
	code, _ = e.do(t, "POST", "/api/v1/send", big)
	if code != 413 {
		t.Fatalf("large body: %d", code)
	}

	e.snd.Cfg.QuietHours = config.QuietHours{Enabled: true, Start: "00:00", End: "23:59", Action: "queue"}
	code, out = e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100004", "text": "night"})
	if now := time.Now(); now.Hour() == 23 && now.Minute() == 59 {
		t.Skip("clock at the edge of the test window")
	}
	if code != 202 || out["queued"] != true || out["scheduled_id"] == nil {
		t.Fatalf("quiet hours: %d %v", code, out)
	}
}

func TestReadListsAndHistory(t *testing.T) {
	e := newEnv(t, false)
	ctx := context.Background()
	group := "120363000000000001@g.us"
	e.db.TouchConversation(ctx, store.Conversation{JID: group, Title: "Book club", IsGroup: true, LastActivity: 200})
	e.db.PutMessage(ctx, &store.Message{Conv: group, ID: "G1", Author: "15550100003@s.whatsapp.net", SentAt: 100, Body: "first"})
	e.db.PutMessage(ctx, &store.Message{Conv: group, ID: "G2", Author: "15550100004@s.whatsapp.net", SentAt: 200, Body: "second"})
	e.db.NoteCall(ctx, store.Call{ID: "C1", Peer: "15550100002@s.whatsapp.net", StartedAt: 150})

	code, out := e.do(t, "GET", "/api/v1/chats?query=book", nil)
	if code != 200 || len(out["chats"].([]any)) != 1 {
		t.Fatalf("chats: %d %v", code, out)
	}
	code, out = e.do(t, "GET", "/api/v1/messages?chat="+group+"&after=150", nil)
	if code != 200 || len(out["messages"].([]any)) != 1 {
		t.Fatalf("messages after: %d %v", code, out)
	}
	code, out = e.do(t, "GET", "/api/v1/messages?before=1970-01-01T00:02:30Z", nil)
	if code != 200 || len(out["messages"].([]any)) != 1 {
		t.Fatalf("messages before RFC3339: %d %v", code, out)
	}
	code, _ = e.do(t, "GET", "/api/v1/messages?after=yesterday", nil)
	if code != 400 {
		t.Fatalf("bad time: %d", code)
	}
	code, out = e.do(t, "GET", "/api/v1/calls?since=100", nil)
	if code != 200 || len(out["calls"].([]any)) != 1 {
		t.Fatalf("calls: %d %v", code, out)
	}

	code, out = e.do(t, "POST", "/api/v1/read", map[string]any{"chat": group, "ids": []string{"G1", "G2"}})
	if code != 200 || out["marked"].(float64) != 2 || len(e.bridge.reads) != 2 {
		t.Fatalf("read: %d %v %v", code, out, e.bridge.reads)
	}
	if m, _ := e.db.GetMessage(ctx, group, "G1"); m.ReadAt == 0 {
		t.Fatal("read_at not stored")
	}
	code, _ = e.do(t, "POST", "/api/v1/read", map[string]any{"chat": group, "ids": []string{"UNKNOWN"}})
	if code != 400 {
		t.Fatalf("unknown group message without author: %d", code)
	}

	code, out = e.do(t, "POST", "/api/v1/history", map[string]any{"chat": group, "count": 500})
	if code != 200 || out["anchor_id"] != "G1" || out["requested"].(float64) != 100 {
		t.Fatalf("history: %d %v", code, out)
	}
	code, _ = e.do(t, "POST", "/api/v1/history", map[string]any{"chat": "15550100009"})
	if code != 404 {
		t.Fatalf("history without messages: %d", code)
	}

	code, out = e.do(t, "POST", "/api/v1/media/download", map[string]any{"chat": group, "id": "IMG"})
	if code != 200 || out["kind"] != "image" {
		t.Fatalf("download: %d %v", code, out)
	}
	code, _ = e.do(t, "POST", "/api/v1/media/download", map[string]any{"chat": group, "id": "nope"})
	if code != 404 {
		t.Fatalf("download missing: %d", code)
	}
	code, _ = e.do(t, "GET", "/api/v1/avatar?jid=15550100002", nil)
	if code != 404 {
		t.Fatalf("avatar: %d", code)
	}
}

func TestScheduleEndpoints(t *testing.T) {
	e := newEnv(t, true)
	at := time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339)
	code, out := e.do(t, "POST", "/api/v1/schedule", map[string]any{
		"to": "15550100002", "text": "Weekly reminder", "at": at, "recurrence": "weekly", "reply_to": "R1",
	})
	if code != 201 {
		t.Fatalf("create: %d %v", code, out)
	}
	id := int(out["id"].(float64))
	code, out = e.do(t, "POST", "/api/v1/schedule", map[string]any{"to": "15550100002", "text": "x", "at": at, "recurrence": "hourly"})
	if code != 400 {
		t.Fatalf("bad recurrence: %d %v", code, out)
	}
	code, out = e.do(t, "GET", "/api/v1/schedule?status=pending", nil)
	list := out["scheduled"].([]any)
	if code != 200 || len(list) != 1 || list[0].(map[string]any)["reply_to_id"] != "R1" {
		t.Fatalf("list: %d %v", code, out)
	}
	code, _ = e.do(t, "DELETE", "/api/v1/schedule/"+strconv.Itoa(id), nil)
	if code != 200 {
		t.Fatalf("cancel: %d", code)
	}
	code, _ = e.do(t, "DELETE", "/api/v1/schedule/"+strconv.Itoa(id), nil)
	if code != 409 {
		t.Fatalf("cancel twice: %d", code)
	}
	code, _ = e.do(t, "DELETE", "/api/v1/schedule/999", nil)
	if code != 404 {
		t.Fatalf("cancel missing: %d", code)
	}
	code, _ = e.do(t, "GET", "/api/v1/schedule?status=weird", nil)
	if code != 400 {
		t.Fatalf("bad status: %d", code)
	}
}

func TestOpenAPIServed(t *testing.T) {
	e := newEnv(t, false)
	req, _ := http.NewRequest("GET", e.srv.URL+"/api/v1/openapi.yaml", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := e.srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := new(bytes.Buffer)
	buf.ReadFrom(resp.Body)
	if resp.StatusCode != 200 || !strings.HasPrefix(buf.String(), "openapi: 3.1") {
		t.Fatalf("openapi: %d", resp.StatusCode)
	}
	for _, p := range []string{"/status:", "/chats:", "/messages:", "/send:", "/read:", "/history:", "/media/download:",
		"/avatar:", "/calls:", "/schedule:", "/schedule/{id}:", "/login/url:", "/openapi.yaml:"} {
		if !strings.Contains(buf.String(), "\n  "+p) {
			t.Errorf("openapi.yaml lacks path %s", p)
		}
	}
}

func TestLoadToken(t *testing.T) {
	cfg := config.Default()
	cfg.Encryption.Mode = config.ModeOff // the plain file; keystore tokens: security_test.go
	cfg.StoreDir = filepath.Join(t.TempDir(), "store")
	tok, err := LoadToken(cfg)
	if err != nil || len(tok) != 64 {
		t.Fatalf("token %q %v", tok, err)
	}
	again, _ := LoadToken(cfg)
	if again != tok {
		t.Fatal("token not reused")
	}
	cfg.API.Token = "short-token"
	if _, err := LoadToken(cfg); err == nil {
		t.Fatal("a short configured token was accepted")
	}
	cfg.API.Token = strings.Repeat("a1", 16)
	if got, _ := LoadToken(cfg); got != strings.Repeat("a1", 16) {
		t.Fatalf("config token ignored: %q", got)
	}
	cfg.API.Token = ""
	os.WriteFile(cfg.TokenFile(), []byte("tooshort\n"), 0o600)
	if got, _ := LoadToken(cfg); len(got) != 64 {
		t.Fatalf("a short token file must be replaced: %q", got)
	}
}

func TestReadCanBeDisabled(t *testing.T) {
	e := newEnv(t, false)
	e.cfg.ReadReceipts.AllowMarkRead = false
	code, out := e.do(t, "POST", "/api/v1/read", map[string]any{"chat": "15550100002", "ids": []string{"A"}})
	if code != 403 || !strings.Contains(out["error"].(string), "allow_mark_read") || len(e.bridge.reads) != 0 {
		t.Fatalf("read while disabled: %d %v", code, out)
	}
	e.cfg.ReadReceipts.AllowMarkRead = true
	if code, _ := e.do(t, "POST", "/api/v1/read", map[string]any{"chat": "15550100002", "ids": []string{"A"}}); code != 200 {
		t.Fatalf("read while enabled: %d", code)
	}
}

func TestLocalErrorsAre500AndUpstreamErrors502(t *testing.T) {
	e := newEnv(t, false)
	e.bridge.readErr = errors.New("disk I/O error")
	if code, _ := e.do(t, "POST", "/api/v1/read", map[string]any{"chat": "15550100002", "ids": []string{"A"}}); code != 500 {
		t.Fatalf("local error: %d", code)
	}
	e.bridge.readErr = common.Upstream("mark read", errors.New("server returned 500"))
	if code, _ := e.do(t, "POST", "/api/v1/read", map[string]any{"chat": "15550100002", "ids": []string{"A"}}); code != 502 {
		t.Fatalf("upstream error: %d", code)
	}
	e.bridge.readErr = wa.ErrNotConnected
	if code, _ := e.do(t, "POST", "/api/v1/read", map[string]any{"chat": "15550100002", "ids": []string{"A"}}); code != 503 {
		t.Fatalf("not connected: %d", code)
	}
}

func TestStoreFilesCannotBeSent(t *testing.T) {
	e := newEnv(t, true)
	e.snd.Cfg.AllowedMediaDirs = []string{e.cfg.Dir} // even a wide allow list
	e.snd.Cfg.DeniedPaths = e.cfg.Sending.DeniedPaths
	code, out := e.do(t, "POST", "/api/v1/send", map[string]any{"to": "15550100002", "media_path": e.cfg.MessagesDB()})
	if code != 403 || !strings.Contains(out["error"].(string), "store") {
		t.Fatalf("store file: %d %v", code, out)
	}
}

func TestMessagesIncludeDeleted(t *testing.T) {
	e := newEnv(t, false)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	e.db.PutMessage(ctx, &store.Message{Conv: conv, ID: "D1", Author: conv, SentAt: 1, Body: "gone"})
	e.db.MarkDeleted(ctx, conv, "D1", conv, 2)
	_, out := e.do(t, "GET", "/api/v1/messages?chat="+conv, nil)
	m := out["messages"].([]any)[0].(map[string]any)
	if m["body"] != "" || m["deleted_at"].(float64) != 2 {
		t.Fatalf("hidden: %v", m)
	}
	_, out = e.do(t, "GET", "/api/v1/messages?chat="+conv+"&include_deleted=true", nil)
	if out["messages"].([]any)[0].(map[string]any)["body"] != "gone" {
		t.Fatalf("include_deleted: %v", out)
	}
}

func TestScheduleKeepsKindAndHuman(t *testing.T) {
	e := newEnv(t, true)
	voice := filepath.Join(e.cfg.Sending.AllowedMediaDirs[0], "note.ogg")
	os.WriteFile(voice, []byte("OggS"), 0o600)
	at := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	code, out := e.do(t, "POST", "/api/v1/schedule", map[string]any{"to": "15550100002", "media_path": voice,
		"as_voice": true, "human": false, "at": at})
	if code != 201 {
		t.Fatalf("create: %d %v", code, out)
	}
	sc := out["scheduled"].(map[string]any)
	if sc["kind"] != "voice" || sc["human"] != false {
		t.Fatalf("stored: %v", sc)
	}
}

func TestLoginURLEndpoint(t *testing.T) {
	e := newEnv(t, false)
	if code, _ := e.do(t, "POST", "/api/v1/login/url", nil); code != 409 {
		t.Fatalf("no login running: %d", code)
	}
	e.bridge.link = "http://127.0.0.1:5000/?t=abc"
	code, out := e.do(t, "POST", "/api/v1/login/url", nil)
	if code != 200 || out["url"] != "http://127.0.0.1:5000/?t=abc" {
		t.Fatalf("login link: %d %v", code, out)
	}
}

func TestMessagesCarryReactions(t *testing.T) {
	e := newEnv(t, false)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	e.db.PutMessage(ctx, &store.Message{Conv: conv, ID: "M1", Author: conv, SentAt: 1, Body: "hi"})
	e.db.SetReaction(ctx, store.Reaction{Conv: conv, MsgID: "M1", Reactor: "15550100003@s.whatsapp.net", Emoji: "ok", At: 2})
	_, out := e.do(t, "GET", "/api/v1/messages?chat="+conv, nil)
	m := out["messages"].([]any)[0].(map[string]any)
	r, _ := m["reactions"].([]any)
	if len(r) != 1 || r[0].(map[string]any)["emoji"] != "ok" {
		t.Fatalf("reactions: %v", m)
	}
}

func TestHostedChatsReadOnly(t *testing.T) {
	e := newEnv(t, true)
	if code, _ := e.do(t, "GET", "/api/v1/messages?chat=123456789@hosted", nil); code != 200 {
		t.Fatalf("read hosted chat: %d", code)
	}
	if code, _ := e.do(t, "POST", "/api/v1/send", map[string]any{"to": "123456789@hosted", "text": "x", "human": false}); code != 400 {
		t.Fatalf("send to hosted: %d", code)
	}
}

func TestMillisecondRule(t *testing.T) {
	e := newEnv(t, false)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	e.db.PutMessage(ctx, &store.Message{Conv: conv, ID: "A", SentAt: 1767323000, Body: "a"})
	e.db.PutMessage(ctx, &store.Message{Conv: conv, ID: "B", SentAt: 1767323100, Body: "b"})
	for _, after := range []string{"1767323050", "1767323050000"} {
		_, out := e.do(t, "GET", "/api/v1/messages?after="+after, nil)
		if list := out["messages"].([]any); len(list) != 1 {
			t.Fatalf("after=%s: %v", after, list)
		}
	}
}

func TestServeWaitsForRunningRequests(t *testing.T) {
	e := newEnv(t, true)
	entered := make(chan struct{})
	release := make(chan struct{})
	s := &Server{Cfg: e.cfg, DB: e.db, WA: e.bridge, Media: fakeMedia{}, Token: token, Version: "test",
		Send: func(ctx context.Context, req sender.Request) (sender.Result, error) {
			close(entered)
			<-release
			if ctx.Err() != nil {
				return sender.Result{}, ctx.Err()
			}
			return sender.Result{ID: "SLOW"}, nil
		}}
	e.cfg.API.Listen = "127.0.0.1:0"
	ctx, cancel := context.WithCancel(context.Background())
	addrc := make(chan string, 1)
	served := make(chan error, 1)
	go func() { served <- s.Serve(ctx, func(addr string) { addrc <- addr }) }()
	addr := <-addrc
	status := make(chan int, 1)
	go func() {
		req, _ := http.NewRequest("POST", "http://"+addr+"/api/v1/send", strings.NewReader(`{"to":"15550100002","text":"slow"}`))
		req.Header.Set("Authorization", "Bearer "+token)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			status <- 0
			return
		}
		resp.Body.Close()
		status <- resp.StatusCode
	}()
	<-entered
	cancel()
	select {
	case <-served:
		t.Fatal("Serve returned while a send was running")
	case <-time.After(300 * time.Millisecond):
	}
	close(release)
	if err := <-served; err != nil {
		t.Fatal(err)
	}
	if code := <-status; code != 200 {
		t.Fatalf("running send was cut off: %d", code)
	}
}

func TestOpenAPIKindsMatchStore(t *testing.T) {
	want := "enum: [" + strings.Join(store.MessageKinds, ", ") + "]"
	if !strings.Contains(string(openAPISpec), "    Kind:") || !strings.Contains(string(openAPISpec), want) {
		t.Fatalf("openapi.yaml Kind enum must be %s", want)
	}
}
