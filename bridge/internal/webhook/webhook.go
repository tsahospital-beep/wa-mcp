// Package webhook delivers bridge events to configured URLs. Each target has
// its own queue and worker; failed deliveries are retried by timers, so a
// slow or failing endpoint never holds up the others. Emit never blocks.
//
// Signature: X-WA-MCP-Signature = "sha256=" + hex(HMAC-SHA256(secret,
// timestamp + "." + delivery id + "." + body)) with timestamp =
// X-WA-MCP-Timestamp (unix seconds) and delivery id = X-WA-MCP-Delivery.
// Receivers should reject timestamps more than 5 minutes away from their
// clock and drop delivery ids they have already processed.
package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

// DefaultDelays are the waits before the three retries.
var DefaultDelays = []time.Duration{time.Second, 5 * time.Second, 25 * time.Second}

// MaxSkew is the freshness window receivers should enforce on the timestamp.
const MaxSkew = 5 * time.Minute

// Options tune the dispatcher; zero values pick defaults.
type Options struct {
	QueueSize int // per target
	Delays    []time.Duration
	Client    *http.Client
	UserAgent string
	Log       *log.Logger
	Now       func() time.Time
	// Done is called after each delivery ends: delivered, or given up. May be nil.
	Done func(event string, delivered bool)
	// Account names the account in every envelope ("" = "default").
	Account string
	// Self returns our masked own number for every envelope. May be nil.
	Self func() string
}

// Dispatcher queues events and posts them, one worker per target.
type Dispatcher struct {
	targets []*target
	opts    Options
	dropped atomic.Int64
	wg      sync.WaitGroup
}

type target struct {
	url    string
	events map[string]bool // empty = all events
	secret []byte          // empty = unsigned
	queue  chan *delivery
}

func (t *target) wants(event string) bool { return len(t.events) == 0 || t.events[event] }

type delivery struct {
	event   string
	id      string
	body    []byte
	attempt int
}

// Envelope is the JSON body of every webhook call.
type Envelope struct {
	Event string `json:"event"`
	At    int64  `json:"at"`
	// Account is the name of the account in "accounts" ("default" without accounts).
	Account string `json:"account"`
	// Self is our own number, masked like in the status ("" before the first link).
	Self string `json:"self"`
	Data any    `json:"data"`
}

// New builds a dispatcher from config. Secrets are read with getenv from the
// variable named in secret_env. The returned notes mention unsigned hooks.
func New(hooks []config.Webhook, getenv func(string) string, opts Options) (*Dispatcher, []string) {
	if opts.QueueSize <= 0 {
		opts.QueueSize = 500
	}
	if opts.Delays == nil {
		opts.Delays = DefaultDelays
	}
	if opts.Client == nil {
		opts.Client = &http.Client{Timeout: 15 * time.Second}
	}
	// Never follow redirects: a 3xx answer counts as a failed delivery.
	client := *opts.Client
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	opts.Client = &client
	if opts.UserAgent == "" {
		opts.UserAgent = "wa-mcp-bridge"
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	d := &Dispatcher{opts: opts}
	var notes []string
	for _, h := range hooks {
		t := &target{url: h.URL, events: map[string]bool{}, queue: make(chan *delivery, opts.QueueSize)}
		for _, e := range h.Events {
			t.events[e] = true
		}
		if h.SecretEnv != "" {
			t.secret = []byte(getenv(h.SecretEnv))
		}
		switch {
		case h.SecretEnv == "":
			notes = append(notes, fmt.Sprintf("webhook %s has no secret_env; requests are not signed", common.RedactURL(h.URL)))
		case len(t.secret) == 0:
			notes = append(notes, fmt.Sprintf("webhook %s: %s is empty; requests are not signed", common.RedactURL(h.URL), h.SecretEnv))
		}
		d.targets = append(d.targets, t)
	}
	return d, notes
}

// Enabled reports whether any webhook is configured.
func (d *Dispatcher) Enabled() bool { return d != nil && len(d.targets) > 0 }

// Start launches one worker per target; they stop when ctx ends.
func (d *Dispatcher) Start(ctx context.Context) {
	if !d.Enabled() {
		return
	}
	for _, t := range d.targets {
		d.wg.Add(1)
		go func(t *target) {
			defer d.wg.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case job := <-t.queue:
					d.attempt(ctx, t, job)
				}
			}
		}(t)
	}
}

// Wait blocks until the workers have stopped.
func (d *Dispatcher) Wait() {
	if d != nil {
		d.wg.Wait()
	}
}

// Dropped counts events lost because a queue was full.
func (d *Dispatcher) Dropped() int64 { return d.dropped.Load() }

// Emit queues an event for every interested target without blocking.
func (d *Dispatcher) Emit(event string, data any) {
	if !d.Enabled() {
		return
	}
	env := Envelope{Event: event, At: d.opts.Now().Unix(), Account: d.opts.Account, Data: data}
	if env.Account == "" {
		env.Account = config.DefaultAccount
	}
	if d.opts.Self != nil {
		env.Self = d.opts.Self()
	}
	body, err := json.Marshal(env)
	if err != nil {
		d.logf("webhook: encode %s: %v", event, err)
		return
	}
	for _, t := range d.targets {
		if t.wants(event) {
			d.enqueue(t, &delivery{event: event, id: newID(), body: body})
		}
	}
}

func (d *Dispatcher) enqueue(t *target, job *delivery) {
	select {
	case t.queue <- job:
	default:
		if n := d.dropped.Add(1); n == 1 || n%100 == 0 {
			d.logf("webhook: queue for %s full, %d events dropped so far", common.RedactURL(t.url), n)
		}
	}
}

func (d *Dispatcher) done(event string, delivered bool) {
	if d.opts.Done != nil {
		d.opts.Done(event, delivered)
	}
}

func (d *Dispatcher) logf(format string, args ...any) {
	if d.opts.Log != nil {
		d.opts.Log.Printf(format, args...)
	}
}

// Sign returns the signature header value for a timestamp, delivery id and body.
func Sign(secret []byte, timestamp, delivery string, body []byte) string {
	m := hmac.New(sha256.New, secret)
	m.Write([]byte(timestamp + "." + delivery + "."))
	m.Write(body)
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

// Verify checks a signature in constant time and rejects timestamps outside
// MaxSkew of now.
func Verify(secret []byte, timestamp, delivery string, body []byte, signature string, now time.Time) bool {
	ts, err := strconv.ParseInt(timestamp, 10, 64)
	if err != nil {
		return false
	}
	if skew := now.Sub(time.Unix(ts, 0)); skew > MaxSkew || skew < -MaxSkew {
		return false
	}
	return hmac.Equal([]byte(Sign(secret, timestamp, delivery, body)), []byte(signature))
}

// attempt posts once; on failure a timer puts the job back into the queue.
func (d *Dispatcher) attempt(ctx context.Context, t *target, job *delivery) {
	err := d.post(ctx, t, job)
	if err == nil {
		d.done(job.event, true)
		return
	}
	if job.attempt >= len(d.opts.Delays) {
		d.logf("webhook: %s to %s failed after %d attempts: %s", job.event, common.RedactURL(t.url), job.attempt+1,
			strings.ReplaceAll(err.Error(), t.url, common.RedactURL(t.url)))
		d.done(job.event, false)
		return
	}
	delay := d.opts.Delays[job.attempt]
	job.attempt++
	time.AfterFunc(delay, func() {
		if ctx.Err() == nil {
			d.enqueue(t, job)
		}
	})
}

func (d *Dispatcher) post(ctx context.Context, t *target, job *delivery) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.url, bytes.NewReader(job.body))
	if err != nil {
		return err
	}
	ts := strconv.FormatInt(d.opts.Now().Unix(), 10)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", d.opts.UserAgent)
	req.Header.Set("X-WA-MCP-Event", job.event)
	req.Header.Set("X-WA-MCP-Delivery", job.id)
	req.Header.Set("X-WA-MCP-Timestamp", ts)
	if len(t.secret) > 0 {
		req.Header.Set("X-WA-MCP-Signature", Sign(t.secret, ts, job.id, job.body))
	}
	resp, err := d.opts.Client.Do(req)
	if err != nil {
		return err
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}

// newID returns a random UUID (version 4).
func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	h := hex.EncodeToString(b[:])
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32]
}
