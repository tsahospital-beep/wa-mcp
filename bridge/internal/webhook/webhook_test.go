package webhook

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

type hit struct {
	body      []byte
	event     string
	signature string
	delivery  string
	timestamp string
}

type recorder struct {
	mu    sync.Mutex
	hits  []hit
	fails int // answer 500 to this many requests first
	done  chan struct{}
	want  int
}

func (r *recorder) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	body, _ := io.ReadAll(req.Body)
	r.mu.Lock()
	r.hits = append(r.hits, hit{body, req.Header.Get("X-WA-MCP-Event"), req.Header.Get("X-WA-MCP-Signature"),
		req.Header.Get("X-WA-MCP-Delivery"), req.Header.Get("X-WA-MCP-Timestamp")})
	n := len(r.hits)
	fail := n <= r.fails
	r.mu.Unlock()
	if fail {
		w.WriteHeader(http.StatusInternalServerError)
	} else {
		w.WriteHeader(http.StatusNoContent)
	}
	if n == r.want {
		close(r.done)
	}
}

func waitFor(t *testing.T, ch chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for webhook calls")
	}
}

var fastDelays = []time.Duration{time.Millisecond, 5 * time.Millisecond, 25 * time.Millisecond}

func TestSignatureWithTimestampAndRetry(t *testing.T) {
	rec := &recorder{fails: 2, want: 3, done: make(chan struct{})}
	srv := httptest.NewServer(rec)
	defer srv.Close()

	env := map[string]string{"HOOK_SECRET": "s3cret"}
	d, notes := New([]config.Webhook{{URL: srv.URL, Events: []string{"message"}, SecretEnv: "HOOK_SECRET"}},
		func(k string) string { return env[k] }, Options{Delays: fastDelays})
	if len(notes) != 0 {
		t.Fatalf("notes: %v", notes)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); d.Wait() }()
	d.Start(ctx)

	d.Emit("call", map[string]any{"ignored": true}) // not subscribed
	d.Emit("message", map[string]any{"conv": "15550100002@s.whatsapp.net", "body": "hi"})
	waitFor(t, rec.done)

	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.hits) != 3 {
		t.Fatalf("hits %d", len(rec.hits))
	}
	uuid := regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	now := time.Now()
	for _, h := range rec.hits {
		if h.event != "message" || h.timestamp == "" {
			t.Fatalf("headers: %+v", h)
		}
		if !Verify([]byte("s3cret"), h.timestamp, h.delivery, h.body, h.signature, now) {
			t.Fatalf("bad signature %q for timestamp %s", h.signature, h.timestamp)
		}
		if h.delivery != rec.hits[0].delivery || !uuid.MatchString(h.delivery) {
			t.Fatalf("delivery id %q", h.delivery)
		}
	}
	h := rec.hits[0]
	// The body alone (old scheme) or another timestamp must not verify.
	if Verify([]byte("s3cret"), "0", h.delivery, h.body, h.signature, now) {
		t.Fatal("signature valid for another timestamp")
	}
	ts, _ := strconv.ParseInt(h.timestamp, 10, 64)
	if Verify([]byte("s3cret"), h.timestamp, h.delivery, h.body, h.signature, time.Unix(ts, 0).Add(6*time.Minute)) {
		t.Fatal("stale timestamp accepted")
	}
	if Verify([]byte("s3cret"), h.timestamp, "00000000-0000-4000-8000-000000000000", h.body, h.signature, now) {
		t.Fatal("signature valid for another delivery id")
	}
	var envl Envelope
	if err := json.Unmarshal(h.body, &envl); err != nil || envl.Event != "message" {
		t.Fatalf("envelope %+v %v", envl, err)
	}
}

func TestGivesUpAfterThreeRetries(t *testing.T) {
	rec := &recorder{fails: 100, want: 4, done: make(chan struct{})}
	srv := httptest.NewServer(rec)
	defer srv.Close()
	finished := make(chan bool, 1)
	var logs strings.Builder
	d, notes := New([]config.Webhook{{URL: srv.URL + "/hook?token=abc"}}, func(string) string { return "" },
		Options{Delays: fastDelays, Log: log.New(&logs, "", 0), Done: func(e string, ok bool) { finished <- ok }})
	if len(notes) != 1 || !strings.Contains(notes[0], "no secret_env") || strings.Contains(notes[0], "token=abc") {
		t.Fatalf("note for a hook without secret_env: %v", notes)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); d.Wait() }()
	d.Start(ctx)
	d.Emit("connection", map[string]any{"state": "connected"})
	select {
	case ok := <-finished:
		if ok {
			t.Fatal("reported as delivered")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("delivery never finished")
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.hits) != 4 || rec.hits[0].signature != "" {
		t.Fatalf("hits %d, signature %q", len(rec.hits), rec.hits[0].signature)
	}
	if out := logs.String(); !strings.Contains(out, "failed after 4 attempts") || strings.Contains(out, "/hook") || strings.Contains(out, "token=abc") {
		t.Fatalf("log must show scheme and host only: %q", out)
	}
}

func TestEmptySecretVariableNote(t *testing.T) {
	_, notes := New([]config.Webhook{{URL: "https://hooks.example.org/x", SecretEnv: "HOOK_SECRET"}}, func(string) string { return "" }, Options{})
	if len(notes) != 1 || !strings.Contains(notes[0], "HOOK_SECRET is empty") || strings.Contains(notes[0], "/x") {
		t.Fatalf("notes: %v", notes)
	}
}
func TestRetryWaitDoesNotBlockOtherEvents(t *testing.T) {
	got := make(chan struct{}, 10)
	healthy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { got <- struct{}{} }))
	defer healthy.Close()
	attempted := make(chan struct{}, 10)
	failingSeen := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempted <- struct{}{}
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer failingSeen.Close()

	d, _ := New([]config.Webhook{{URL: failingSeen.URL}, {URL: healthy.URL}}, func(string) string { return "" },
		Options{Delays: []time.Duration{time.Hour, time.Hour, time.Hour}})
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); d.Wait() }()
	d.Start(ctx)
	d.Emit("message", 1)
	d.Emit("message", 2)
	// Both events reach both targets although the first target's retries wait an hour.
	for i := 0; i < 2; i++ {
		for _, ch := range []chan struct{}{attempted, got} {
			select {
			case <-ch:
			case <-time.After(5 * time.Second):
				t.Fatalf("event %d did not arrive", i+1)
			}
		}
	}
}
func TestRedirectsAreNotFollowed(t *testing.T) {
	var elsewhere atomic.Int32
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { elsewhere.Add(1) }))
	defer other.Close()
	rec := &recorder{want: 4, done: make(chan struct{})}
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.mu.Lock()
		rec.hits = append(rec.hits, hit{})
		n := len(rec.hits)
		rec.mu.Unlock()
		http.Redirect(w, r, other.URL, http.StatusTemporaryRedirect)
		if n == rec.want {
			close(rec.done)
		}
	}))
	defer redirect.Close()
	d, _ := New([]config.Webhook{{URL: redirect.URL}}, func(string) string { return "" }, Options{Delays: fastDelays})
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); d.Wait() }()
	d.Start(ctx)
	d.Emit("message", "x")
	waitFor(t, rec.done)
	if elsewhere.Load() != 0 {
		t.Fatalf("redirect followed %d times", elsewhere.Load())
	}
}

func TestEmitNeverBlocks(t *testing.T) {
	d, _ := New([]config.Webhook{{URL: "http://127.0.0.1:1/never"}}, func(string) string { return "" }, Options{QueueSize: 2})
	// Workers are not started, so the queue fills up.
	done := make(chan struct{})
	go func() {
		for i := 0; i < 50; i++ {
			d.Emit("message", i)
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Emit blocked")
	}
	if d.Dropped() != 48 {
		t.Fatalf("dropped %d", d.Dropped())
	}
	var none *Dispatcher
	none.Emit("message", nil) // nil dispatcher is a no-op
}

func TestSignCoversTimestampDeliveryAndBody(t *testing.T) {
	m := hmac.New(sha256.New, []byte("key"))
	m.Write([]byte("1700000000.d-1.{\"event\":\"message\"}"))
	want := "sha256=" + hex.EncodeToString(m.Sum(nil))
	if got := Sign([]byte("key"), "1700000000", "d-1", []byte(`{"event":"message"}`)); got != want {
		t.Fatalf("got %s want %s", got, want)
	}
	if Verify([]byte("key"), "1700000000", "d-1", []byte(`{"event":"message"}`), want, time.Unix(1700000000+60, 0)) != true {
		t.Fatal("fresh signature rejected")
	}
	if Verify([]byte("key"), "not-a-number", "d-1", nil, want, time.Now()) {
		t.Fatal("bad timestamp accepted")
	}
}
