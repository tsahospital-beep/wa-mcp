package sender

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
	_ "time/tzdata"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

var bg = context.Background()

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

type sentMsg struct {
	to  types.JID
	msg *waE2E.Message
}

type fakeTransport struct {
	mu       sync.Mutex
	sent     []sentMsg
	presence []string
	reads    [][]string
	readFrom []types.JID
	uploads  []whatsmeow.MediaType
	clock    *fakeClock
	n        int
}

func (f *fakeTransport) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.n++
	f.sent = append(f.sent, sentMsg{to, msg})
	return "OUT" + string(rune('A'+f.n-1)), f.clock.Now(), nil
}

func (f *fakeTransport) Upload(ctx context.Context, r io.Reader, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
	data, err := io.ReadAll(r)
	if err != nil {
		return whatsmeow.UploadResponse{}, err
	}
	f.mu.Lock()
	f.uploads = append(f.uploads, kind)
	f.mu.Unlock()
	sum := sha256.Sum256(data)
	return whatsmeow.UploadResponse{URL: "https://mmg.whatsapp.net/v/x.enc", DirectPath: "/v/x.enc", MediaKey: []byte{1, 2},
		FileSHA256: sum[:], FileEncSHA256: []byte{3}, FileLength: uint64(len(data))}, nil
}

func (f *fakeTransport) ChatPresence(ctx context.Context, to types.JID, state types.ChatPresence, media types.ChatPresenceMedia) error {
	f.mu.Lock()
	f.presence = append(f.presence, string(state)+"/"+string(media))
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) MarkRead(ctx context.Context, ids []string, at time.Time, chat, sender types.JID) error {
	f.mu.Lock()
	f.reads = append(f.reads, ids)
	f.readFrom = append(f.readFrom, sender)
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) OwnJID() types.JID {
	return types.NewJID("15550100001", types.DefaultUserServer)
}

type rig struct {
	s      *Sender
	tr     *fakeTransport
	clock  *fakeClock
	sleeps []time.Duration
	db     *store.DB
}

func newRig(t *testing.T, mutate func(*config.Sending)) *rig {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	cfg := config.Default().Sending
	cfg.Enabled = true
	if mutate != nil {
		mutate(&cfg)
	}
	r := &rig{clock: &fakeClock{t: time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)}, db: db}
	r.tr = &fakeTransport{clock: r.clock}
	r.s = &Sender{
		Cfg: cfg, DB: db, Transport: r.tr, Clock: r.clock, Location: time.UTC,
		Rand: func() float64 { return 0.5 },
		Sleep: func(ctx context.Context, d time.Duration) error {
			r.sleeps = append(r.sleeps, d)
			r.clock.Advance(d)
			return nil
		},
	}
	return r
}

const bob = "15550100002@s.whatsapp.net"

func noHuman() *bool { f := false; return &f }

func TestSendingDisabled(t *testing.T) {
	r := newRig(t, func(c *config.Sending) { c.Enabled = false })
	if _, err := r.s.Send(bg, Request{To: bob, Text: "hi"}); !errors.Is(err, ErrDisabled) {
		t.Fatalf("want ErrDisabled, got %v", err)
	}
	if len(r.tr.sent) != 0 {
		t.Fatal("message went out")
	}
}

func TestLimits(t *testing.T) {
	r := newRig(t, func(c *config.Sending) {
		c.Limits = config.Limits{PerChatPerHour: 2, PerHour: 3, PerDay: 4}
		c.DedupeWindowMinutes = 0
	})
	send := func(to, text string) error {
		_, err := r.s.Send(bg, Request{To: to, Text: text, Human: noHuman()})
		return err
	}
	if err := send(bob, "1"); err != nil {
		t.Fatal(err)
	}
	if err := send(bob, "2"); err != nil {
		t.Fatal(err)
	}
	var le *LimitError
	if err := send(bob, "3"); !errors.As(err, &le) || le.Scope != "per_chat_per_hour" {
		t.Fatalf("want per_chat_per_hour, got %v", err)
	}
	if err := send("15550100003", "4"); err != nil {
		t.Fatal(err)
	}
	if err := send("15550100004", "5"); !errors.As(err, &le) || le.Scope != "per_hour" {
		t.Fatalf("want per_hour, got %v", err)
	}
	r.clock.Advance(61 * time.Minute)
	if err := send("15550100004", "6"); err != nil {
		t.Fatal(err)
	}
	if err := send("15550100005", "7"); !errors.As(err, &le) || le.Scope != "per_day" {
		t.Fatalf("want per_day, got %v", err)
	}
	if len(r.tr.sent) != 4 {
		t.Fatalf("sent %d messages", len(r.tr.sent))
	}
}

func TestDedupe(t *testing.T) {
	r := newRig(t, nil)
	if _, err := r.s.Send(bg, Request{To: bob, Text: "See you at the book club", Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	if _, err := r.s.Send(bg, Request{To: "+1 555 010 0002", Text: "See you at the book club", Human: noHuman()}); !errors.Is(err, ErrDuplicate) {
		t.Fatalf("want ErrDuplicate, got %v", err)
	}
	if _, err := r.s.Send(bg, Request{To: "15550100003", Text: "See you at the book club", Human: noHuman()}); err != nil {
		t.Fatalf("other chat: %v", err)
	}
	r.clock.Advance(11 * time.Minute)
	if _, err := r.s.Send(bg, Request{To: bob, Text: "See you at the book club", Human: noHuman()}); err != nil {
		t.Fatalf("after window: %v", err)
	}
}

func TestQuietHours(t *testing.T) {
	r := newRig(t, func(c *config.Sending) {
		c.QuietHours = config.QuietHours{Enabled: true, Start: "23:00", End: "08:00", Action: "queue"}
	})
	r.clock.t = time.Date(2026, 5, 4, 23, 30, 0, 0, time.UTC)
	res, err := r.s.Send(bg, Request{To: bob, Text: "late note", ReplyTo: &Reply{ID: "X1"}})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Queued || res.ScheduledID == 0 || len(r.tr.sent) != 0 {
		t.Fatalf("not queued: %+v", res)
	}
	sc, _ := r.db.GetScheduled(bg, res.ScheduledID)
	want := time.Date(2026, 5, 5, 8, 0, 0, 0, time.UTC).Unix()
	if sc.NextRun != want || sc.Body != "late note" || sc.ReplyToID != "X1" || sc.Recurrence != "once" {
		t.Fatalf("schedule %+v, want next_run %d", sc, want)
	}

	// Early morning is inside the window too.
	in, until := r.s.InQuietHours(time.Date(2026, 5, 5, 7, 59, 0, 0, time.UTC))
	if !in || until.Hour() != 8 || until.Day() != 5 {
		t.Fatalf("07:59: %v %v", in, until)
	}
	if in, _ := r.s.InQuietHours(time.Date(2026, 5, 5, 8, 0, 0, 0, time.UTC)); in {
		t.Fatal("08:00 must be outside")
	}

	// Scheduled sends were timed on purpose and go out.
	if _, err := r.s.Send(bg, Request{To: bob, Text: "scheduled", Scheduled: true, Human: noHuman()}); err != nil || len(r.tr.sent) != 1 {
		t.Fatalf("scheduled send: %v", err)
	}

	r.s.Cfg.QuietHours.Action = "refuse"
	if _, err := r.s.Send(bg, Request{To: bob, Text: "refused"}); !errors.Is(err, ErrQuietHours) {
		t.Fatalf("want ErrQuietHours, got %v", err)
	}

	// Daytime window that does not wrap.
	r.s.Cfg.QuietHours = config.QuietHours{Enabled: true, Start: "12:00", End: "13:00", Action: "refuse"}
	if in, _ := r.s.InQuietHours(time.Date(2026, 5, 5, 12, 30, 0, 0, time.UTC)); !in {
		t.Fatal("12:30 must be inside 12:00-13:00")
	}
	if in, _ := r.s.InQuietHours(time.Date(2026, 5, 5, 23, 30, 0, 0, time.UTC)); in {
		t.Fatal("23:30 must be outside 12:00-13:00")
	}
}

func TestTypingTimeBounds(t *testing.T) {
	r := newRig(t, nil)
	cfg := r.s.Cfg // cps 7, min 1.2, max 20, jitter 0.3
	for _, chars := range []int{0, 3, 70, 500, 5000} {
		for _, u := range []float64{0, 0.25, 0.5, 0.75, 0.999999} {
			r.s.Rand = func() float64 { return u }
			d := r.s.TypingTime(chars).Seconds()
			base := max(cfg.TypingMinSeconds, min(float64(chars)/cfg.TypingCharsPerSecond, cfg.TypingMaxSeconds))
			lo, hi := base*(1-cfg.Jitter), base*(1+cfg.Jitter)
			if d < lo-1e-6 || d > hi+1e-6 {
				t.Fatalf("chars=%d u=%v: %.3fs outside [%.3f, %.3f]", chars, u, d, lo, hi)
			}
		}
	}
	r.s.Rand = func() float64 { return 0.5 }
	if got := r.s.TypingTime(70).Seconds(); got < 9.99 || got > 10.01 {
		t.Fatalf("70 chars at 7 cps without jitter: %.3f", got)
	}
	if got := r.s.TypingTime(5000).Seconds(); got > 20.01 {
		t.Fatalf("long text not clamped: %.3f", got)
	}
	if got := r.s.TypingTime(1).Seconds(); got < 1.19 {
		t.Fatalf("short text below minimum: %.3f", got)
	}
	r.s.Rand = func() float64 { return 0 }
	if got := r.s.TypingTime(5000).Seconds(); got < 13.99 || got > 14.01 {
		t.Fatalf("max with -jitter: %.3f", got)
	}
}

func TestHumanPipeline(t *testing.T) {
	r := newRig(t, nil)
	// Two unread incoming messages after our last reply.
	r.db.PutMessage(bg, &store.Message{Conv: bob, ID: "IN1", Author: bob, SentAt: 100, Body: "are you there?"})
	r.db.PutMessage(bg, &store.Message{Conv: bob, ID: "IN2", Author: bob, SentAt: 101, Body: "hello?"})

	res, err := r.s.Send(bg, Request{To: bob, Text: "Yes, here"}) // 9 chars
	if err != nil {
		t.Fatal(err)
	}
	if len(r.tr.reads) != 1 || len(r.tr.reads[0]) != 2 {
		t.Fatalf("mark read: %v", r.tr.reads)
	}
	if m, _ := r.db.GetMessage(bg, bob, "IN1"); m.ReadAt == 0 {
		t.Fatal("read_at not stored")
	}
	// reading pause (0.4 + 1.1*0.5) then typing clamp(9/7)=1.2857s
	if len(r.sleeps) != 2 || r.sleeps[0] != seconds(0.95) || r.sleeps[1] != seconds(9.0/7.0) {
		t.Fatalf("sleeps %v", r.sleeps)
	}
	if len(r.tr.presence) != 2 || r.tr.presence[0] != "composing/" || r.tr.presence[1] != "paused/" {
		t.Fatalf("presence %v", r.tr.presence)
	}
	if res.Waited != r.sleeps[0]+r.sleeps[1] || res.ID == "" {
		t.Fatalf("result %+v", res)
	}
	got, err := r.db.GetMessage(bg, bob, res.ID)
	if err != nil || !got.Outgoing || got.Body != "Yes, here" || got.Author != "15550100001@s.whatsapp.net" {
		t.Fatalf("stored sent message: %+v %v", got, err)
	}

	// The next message to the same chat waits the pause between messages first.
	r.sleeps = nil
	if _, err := r.s.Send(bg, Request{To: bob, Text: "One more thing"}); err != nil {
		t.Fatal(err)
	}
	if len(r.sleeps) != 3 || r.sleeps[0] != seconds(2.25) {
		t.Fatalf("pause between messages: %v", r.sleeps)
	}
	// Nothing left to mark read.
	if len(r.tr.reads) != 1 {
		t.Fatalf("mark read again: %v", r.tr.reads)
	}
}

func TestHumanFalseSkipsWaitsButNotLimits(t *testing.T) {
	r := newRig(t, func(c *config.Sending) { c.Limits.PerChatPerHour = 1 })
	res, err := r.s.Send(bg, Request{To: bob, Text: "quick", Human: noHuman()})
	if err != nil {
		t.Fatal(err)
	}
	if res.Waited != 0 || len(r.sleeps) != 0 || len(r.tr.presence) != 0 {
		t.Fatalf("waited %v sleeps %v presence %v", res.Waited, r.sleeps, r.tr.presence)
	}
	var le *LimitError
	if _, err := r.s.Send(bg, Request{To: bob, Text: "second", Human: noHuman()}); !errors.As(err, &le) {
		t.Fatalf("limit skipped: %v", err)
	}

	r2 := newRig(t, func(c *config.Sending) { c.HumanLike = false })
	if res, err := r2.s.Send(bg, Request{To: bob, Text: "config says no waits"}); err != nil || res.Waited != 0 {
		t.Fatalf("human_like=false: %+v %v", res, err)
	}
	if _, err := r2.s.Send(bg, Request{To: bob, Text: "config says no waits"}); !errors.Is(err, ErrDuplicate) {
		t.Fatalf("dedupe skipped: %v", err)
	}
}

func TestVoiceNotePipeline(t *testing.T) {
	r := newRig(t, nil)
	dir := t.TempDir()
	voice := filepath.Join(dir, "note.ogg")
	os.WriteFile(voice, syntheticOpus(), 0o600)

	res, err := r.s.Send(bg, Request{To: bob, Text: "listen", MediaPath: voice, AsVoice: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.tr.sent) != 2 || len(res.IDs) != 2 {
		t.Fatalf("want text + voice, got %d", len(r.tr.sent))
	}
	if r.tr.sent[0].msg.GetConversation() != "listen" {
		t.Fatalf("first part: %v", r.tr.sent[0].msg)
	}
	a := r.tr.sent[1].msg.GetAudioMessage()
	if a == nil || !a.GetPTT() || a.GetSeconds() != 3 || len(a.GetWaveform()) != 64 || a.GetMimetype() != "audio/ogg; codecs=opus" {
		t.Fatalf("voice message: %v", a)
	}
	if r.tr.presence[len(r.tr.presence)-2] != "composing/audio" {
		t.Fatalf("recording presence missing: %v", r.tr.presence)
	}
	stored, _ := r.db.GetMessage(bg, bob, res.IDs[1])
	if stored.Kind != "voice" || stored.LocalPath == "" || len(stored.MediaKey) == 0 {
		t.Fatalf("stored voice: %+v", stored)
	}

	notOpus := filepath.Join(dir, "song.mp3")
	os.WriteFile(notOpus, []byte("ID3"), 0o600)
	if _, err := r.s.Send(bg, Request{To: bob, MediaPath: notOpus, AsVoice: true}); !errors.Is(err, ErrNotOpus) {
		t.Fatalf("mp3 as voice: %v", err)
	}
}

func TestAllowedMediaDirs(t *testing.T) {
	allowed := t.TempDir()
	other := t.TempDir()
	in := filepath.Join(allowed, "photo.jpg")
	out := filepath.Join(other, "photo.jpg")
	os.WriteFile(in, []byte("jpg"), 0o600)
	os.WriteFile(out, []byte("jpg"), 0o600)

	if _, err := CheckPath(in, []string{allowed}, nil); err != nil {
		t.Fatalf("inside: %v", err)
	}
	var pe *PathError
	if _, err := CheckPath(out, []string{allowed}, nil); !errors.As(err, &pe) || !pe.Forbidden() {
		t.Fatalf("outside accepted: %v", err)
	}
	if _, err := CheckPath(filepath.Join(allowed, "missing.jpg"), nil, nil); !errors.As(err, &pe) || pe.Forbidden() {
		t.Fatalf("missing accepted: %v", err)
	}
	if _, err := CheckPath(allowed, nil, nil); !errors.As(err, &pe) {
		t.Fatalf("directory accepted: %v", err)
	}
	if _, err := CheckPath(filepath.Join(allowed, "..", filepath.Base(other), "photo.jpg"), []string{allowed}, nil); !errors.As(err, &pe) {
		t.Fatalf("dot-dot escape accepted: %v", err)
	}
}

func TestStoreAndConfigAreNeverSent(t *testing.T) {
	root := t.TempDir()
	storeDir := filepath.Join(root, "store")
	os.MkdirAll(storeDir, 0o700)
	db := filepath.Join(storeDir, "wa.db")
	token := filepath.Join(storeDir, "api.token")
	cfgFile := filepath.Join(root, "config.json")
	photo := filepath.Join(root, "photo.jpg")
	for _, p := range []string{db, token, cfgFile, photo} {
		os.WriteFile(p, []byte("x"), 0o600)
	}
	allowed := []string{root} // even when the allow list covers them
	denied := []string{storeDir, cfgFile}
	var pe *PathError
	for _, p := range []string{db, token, cfgFile} {
		if _, err := CheckPath(p, allowed, denied); !errors.As(err, &pe) || !pe.Forbidden() {
			t.Errorf("%s accepted: %v", filepath.Base(p), err)
		}
	}
	if runtime.GOOS == "windows" {
		if _, err := CheckPath(strings.ToUpper(db), allowed, denied); err == nil {
			t.Error("upper-case path to the store accepted")
		}
	}
	if _, err := CheckPath(photo, allowed, denied); err != nil {
		t.Fatalf("normal file refused: %v", err)
	}
	link := filepath.Join(root, "innocent.jpg")
	if err := os.Symlink(db, link); err == nil {
		if _, err := CheckPath(link, allowed, denied); err == nil {
			t.Error("symlink into the store accepted")
		}
	}
}

func TestRequestFromSchedule(t *testing.T) {
	no := false
	cases := []struct {
		kind, media string
		ok          func(Request) bool
	}{
		{"", "", func(r Request) bool { return r.MediaPath == "" && r.StickerPath == "" }},
		{"", "/x/a.webp", func(r Request) bool { return r.StickerPath == "/x/a.webp" }},
		{"", "/x/b.ogg", func(r Request) bool { return r.MediaPath == "/x/b.ogg" && r.AsVoice }},
		{"", "/x/c.pdf", func(r Request) bool { return r.MediaPath == "/x/c.pdf" && !r.AsVoice }},
		{"audio", "/x/song.ogg", func(r Request) bool { return r.MediaPath == "/x/song.ogg" && !r.AsVoice }},
		{"image", "/x/pic.webp", func(r Request) bool { return r.MediaPath == "/x/pic.webp" && r.StickerPath == "" }},
		{"voice", "/x/v.opus", func(r Request) bool { return r.AsVoice }},
		{"sticker", "/x/s.webp", func(r Request) bool { return r.StickerPath == "/x/s.webp" }},
	}
	for _, c := range cases {
		req := RequestFromSchedule(&store.Scheduled{Target: bob, Body: "b", MediaPath: c.media, Kind: c.kind, ReplyToID: "R", Human: &no})
		if !c.ok(req) || !req.Scheduled || req.ReplyTo == nil || req.ReplyTo.ID != "R" || req.Human == nil || *req.Human {
			t.Errorf("%s %q: %+v", c.kind, c.media, req)
		}
	}
}

func TestQueuedSendKeepsKindAndHuman(t *testing.T) {
	r := newRig(t, func(c *config.Sending) {
		c.QuietHours = config.QuietHours{Enabled: true, Start: "22:00", End: "07:00", Action: "queue"}
	})
	r.clock.t = time.Date(2026, 5, 4, 23, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	song := filepath.Join(dir, "song.ogg")
	os.WriteFile(song, syntheticOpus(), 0o600)
	res, err := r.s.Send(bg, Request{To: bob, Text: "listen later", MediaPath: song, Human: noHuman()})
	if err != nil || !res.Queued {
		t.Fatalf("%+v %v", res, err)
	}
	sc, _ := r.db.GetScheduled(bg, res.ScheduledID)
	if sc.Kind != "audio" || sc.Human == nil || *sc.Human {
		t.Fatalf("stored kind/human: %+v", sc)
	}
	req := RequestFromSchedule(sc)
	if req.AsVoice || req.MediaPath != song || *req.Human {
		t.Fatalf("an approved audio file must not turn into a voice note: %+v", req)
	}
	// The reservation made before queueing is given back.
	if n, _ := r.db.CountSent(bg, "", 0); n != 0 {
		t.Fatalf("outbox rows after queueing: %d", n)
	}
	if _, err := r.s.Send(bg, Request{To: bob, MediaPath: song, StickerPath: song}); err == nil {
		t.Fatal("media plus sticker queued")
	}
}

type slowTransport struct {
	fakeTransport
	delay time.Duration
	fail  bool
}

func (s *slowTransport) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	time.Sleep(s.delay)
	if s.fail {
		return "", time.Time{}, errors.New("network down")
	}
	return s.fakeTransport.SendMessage(ctx, to, msg)
}

func TestConcurrentSendsRespectLimits(t *testing.T) {
	r := newRig(t, func(c *config.Sending) { c.Limits = config.Limits{PerHour: 3}; c.HumanLike = false })
	slow := &slowTransport{fakeTransport: fakeTransport{clock: r.clock}, delay: 20 * time.Millisecond}
	r.s.Transport = slow
	var wg sync.WaitGroup
	var mu sync.Mutex
	okCount, limited := 0, 0
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := r.s.Send(bg, Request{To: "1555010010" + string(rune('0'+i)), Text: "hello"})
			mu.Lock()
			defer mu.Unlock()
			var le *LimitError
			switch {
			case err == nil:
				okCount++
			case errors.As(err, &le):
				limited++
			default:
				t.Errorf("unexpected error: %v", err)
			}
		}(i)
	}
	wg.Wait()
	if okCount != 3 || limited != 7 {
		t.Fatalf("sent %d, limited %d; want 3 and 7", okCount, limited)
	}
}

func TestFailedSendReleasesSlot(t *testing.T) {
	r := newRig(t, nil)
	r.s.Transport = &slowTransport{fakeTransport: fakeTransport{clock: r.clock}, fail: true}
	if _, err := r.s.Send(bg, Request{To: bob, Text: "x", Human: noHuman()}); err == nil {
		t.Fatal("send must fail")
	}
	if n, _ := r.db.CountSent(bg, "", 0); n != 0 {
		t.Fatalf("slot not released: %d rows", n)
	}
	// Dedupe does not block a retry after a failed send.
	r.s.Transport = r.tr
	if _, err := r.s.Send(bg, Request{To: bob, Text: "x", Human: noHuman()}); err != nil {
		t.Fatalf("retry: %v", err)
	}
}

func TestBodyHashIsKeyed(t *testing.T) {
	r := newRig(t, nil)
	if _, err := r.s.Send(bg, Request{To: bob, Text: "secret plan", Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	var stored string
	r.db.SQL().QueryRow(`SELECT body_hash FROM outbox_log`).Scan(&stored)
	plain := sha256.Sum256([]byte("secret plan\x00\x00\x00false"))
	if stored == hex.EncodeToString(plain[:]) {
		t.Fatal("body hash is an unkeyed SHA-256")
	}
	key, _ := r.db.OutboxKey(bg)
	if stored != BodyHash(key, "secret plan", "", "", false) {
		t.Fatal("body hash does not use the store key")
	}
	if BodyHash([]byte("other key"), "secret plan", "", "", false) == stored {
		t.Fatal("hash must depend on the key")
	}
}

func TestEncryptedStoreKeepsNoLocalPathForSentMedia(t *testing.T) {
	r := newRig(t, nil)
	r.db.SetupEncryption(bg, true, "pw")
	doc := filepath.Join(t.TempDir(), "salary review.pdf")
	os.WriteFile(doc, []byte("%PDF"), 0o600)
	res, err := r.s.Send(bg, Request{To: bob, MediaPath: doc, Human: noHuman()})
	if err != nil {
		t.Fatal(err)
	}
	var local sql.NullString
	r.db.SQL().QueryRow(`SELECT local_path FROM chat_messages WHERE msg_id = ?`, res.ID).Scan(&local)
	if local.Valid {
		t.Fatalf("local_path stored in an encrypted store: %q", local.String)
	}
}

func TestLogsHideNumbersAndText(t *testing.T) {
	r := newRig(t, func(c *config.Sending) {
		c.QuietHours = config.QuietHours{Enabled: true, Start: "22:00", End: "07:00", Action: "queue"}
	})
	var buf strings.Builder
	r.s.Log = log.New(&buf, "", 0)
	r.clock.t = time.Date(2026, 5, 4, 23, 0, 0, 0, time.UTC)
	if _, err := r.s.Send(bg, Request{To: bob, Text: "meet at the library"}); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	if out == "" || strings.Contains(out, "15550100002") || strings.Contains(out, "library") {
		t.Fatalf("log line leaks data: %q", out)
	}
}

func TestHardLinksAndJunctionsRefused(t *testing.T) {
	root := t.TempDir()
	storeDir := filepath.Join(root, "store")
	allowed := filepath.Join(root, "downloads")
	os.MkdirAll(storeDir, 0o700)
	os.MkdirAll(allowed, 0o700)
	session := filepath.Join(storeDir, "session.db")
	os.WriteFile(session, []byte("SECRET"), 0o600)
	var pe *PathError

	hl := filepath.Join(allowed, "photo.jpg")
	if err := os.Link(session, hl); err != nil {
		t.Skipf("hard links not available: %v", err)
	}
	if _, err := CheckPath(hl, []string{allowed}, []string{storeDir}); !errors.As(err, &pe) || !pe.Forbidden() {
		t.Fatalf("hard link to the session accepted: %v", err)
	}
	// Even a single-link copy elsewhere that is the same file is caught by SameFile.
	if runtime.GOOS == "windows" {
		j := filepath.Join(allowed, "j")
		if out, err := exec.Command("cmd", "/c", "mklink", "/J", j, storeDir).CombinedOutput(); err == nil {
			if _, err := CheckPath(filepath.Join(j, "session.db"), []string{allowed}, []string{storeDir}); !errors.As(err, &pe) || !pe.Forbidden() {
				t.Fatalf("junction into the store accepted: %v", err)
			}
		} else {
			t.Logf("junction not created: %s", out)
		}
	}
	plain := filepath.Join(allowed, "real.jpg")
	os.WriteFile(plain, []byte("jpg"), 0o600)
	if _, err := CheckPath(plain, []string{allowed}, []string{storeDir}); err != nil {
		t.Fatalf("normal file refused: %v", err)
	}
	if err := (&PathError{Path: `C:\Users\someone\secret\plan.pdf`, Reason: "x"}).Error(); strings.Contains(err, "secret") {
		t.Fatalf("path error shows folders: %s", err)
	}
}

func TestUploadUsesTheCheckedHandle(t *testing.T) {
	r := newRig(t, nil)
	dir := t.TempDir()
	doc := filepath.Join(dir, "notes.pdf")
	other := filepath.Join(dir, "other.pdf")
	os.WriteFile(doc, []byte("original"), 0o600)
	os.WriteFile(other, []byte("swapped"), 0o600)
	f, err := OpenMedia(doc, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	// Swapping another file in under the checked name (refused on Windows while
	// the handle is open) does not change what is uploaded.
	if err := os.Rename(other, doc); err != nil {
		t.Logf("swap refused by the system: %v", err)
	}
	_, row, _, err := r.s.build(bg, part{kind: store.KindDocument, file: f}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if row.ByteSize != int64(len("original")) {
		t.Fatalf("uploaded %d bytes, want the checked file", row.ByteSize)
	}
}

type cancelTransport struct {
	fakeTransport
	cancel       context.CancelFunc
	errAfterSend bool
}

func (c *cancelTransport) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	c.cancel()
	if ctx.Err() != nil {
		return "", time.Time{}, ctx.Err()
	}
	if c.errAfterSend {
		c.fakeTransport.SendMessage(ctx, to, msg)
		return "", time.Time{}, context.DeadlineExceeded
	}
	return c.fakeTransport.SendMessage(ctx, to, msg)
}

func TestCancelAroundSend(t *testing.T) {
	r := newRig(t, nil)
	ctx, cancel := context.WithCancel(bg)
	r.s.Transport = &cancelTransport{fakeTransport: fakeTransport{clock: r.clock}, cancel: cancel}
	res, err := r.s.Send(ctx, Request{To: bob, Text: "went out", Human: noHuman()})
	if err != nil {
		t.Fatalf("caller cancel must not abort the send itself: %v", err)
	}
	if _, err := r.db.GetMessage(bg, bob, res.ID); err != nil {
		t.Fatalf("sent message not recorded: %v", err)
	}

	ctx2, cancel2 := context.WithCancel(bg)
	ct := &cancelTransport{fakeTransport: fakeTransport{clock: r.clock}, cancel: cancel2, errAfterSend: true}
	r.s.Transport = ct
	if _, err := r.s.Send(ctx2, Request{To: bob, Text: "maybe out", Human: noHuman()}); err == nil || !strings.Contains(err.Error(), "may have been delivered") {
		t.Fatalf("lost answer: %v", err)
	}
	r.s.Transport = r.tr
	if _, err := r.s.Send(bg, Request{To: bob, Text: "maybe out", Human: noHuman()}); !errors.Is(err, ErrDuplicate) {
		t.Fatalf("retry after a lost answer must be refused as a duplicate: %v", err)
	}
}

func TestQuietHoursOnDSTDays(t *testing.T) {
	loc, err := time.LoadLocation("America/New_York")
	if err != nil {
		t.Fatal(err)
	}
	r := newRig(t, func(c *config.Sending) {
		c.QuietHours = config.QuietHours{Enabled: true, Start: "23:00", End: "08:00", Action: "refuse"}
	})
	r.s.Location = loc
	cases := []struct {
		at    time.Time
		quiet bool
		until time.Time
	}{
		{time.Date(2026, 3, 8, 8, 30, 0, 0, loc), false, time.Time{}},                           // spring forward, after 08:00
		{time.Date(2026, 3, 8, 7, 30, 0, 0, loc), true, time.Date(2026, 3, 8, 8, 0, 0, 0, loc)}, // spring forward, before 08:00
		{time.Date(2026, 11, 1, 7, 30, 0, 0, loc), true, time.Date(2026, 11, 1, 8, 0, 0, 0, loc)},
		{time.Date(2026, 11, 1, 8, 30, 0, 0, loc), false, time.Time{}},
		{time.Date(2026, 3, 7, 23, 30, 0, 0, loc), true, time.Date(2026, 3, 8, 8, 0, 0, 0, loc)},
	}
	for _, c := range cases {
		in, until := r.s.InQuietHours(c.at)
		if in != c.quiet || (c.quiet && !until.Equal(c.until)) {
			t.Errorf("%v: quiet=%v until=%v, want %v %v", c.at, in, until, c.quiet, c.until)
		}
	}
}

func TestMarkReadSwitch(t *testing.T) {
	r := newRig(t, nil)
	r.s.NoMarkRead = true
	r.db.PutMessage(bg, &store.Message{Conv: bob, ID: "IN1", Author: bob, SentAt: 100, Body: "hello?"})
	if _, err := r.s.Send(bg, Request{To: bob, Text: "here", Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	if len(r.tr.reads) != 0 {
		t.Fatalf("marked read although read_receipts.allow_mark_read is false: %v", r.tr.reads)
	}
}
