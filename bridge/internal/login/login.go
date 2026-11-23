// Package login serves the login window: a small local page (127.0.0.1 only)
// that shows the pairing QR code or a phone-number pairing code, opened as an
// app-style browser window.
package login

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	qrcode "github.com/skip2/go-qrcode"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
)

//go:embed web/login.html
var assets embed.FS

type state struct {
	Phase     string `json:"phase"` // starting | qr | expired | code | connected | error
	Seq       int    `json:"seq"`
	SVG       string `json:"svg,omitempty"`
	ExpiresIn int64  `json:"expires_in_ms,omitempty"`
	Total     int64  `json:"total_ms,omitempty"`
	PairCode  string `json:"pair_code,omitempty"`
	Phone     string `json:"phone,omitempty"`
	Error     string `json:"error,omitempty"`
}

// PairFunc requests a phone-number pairing code for phone (digits only).
type PairFunc = func(ctx context.Context, phone string) (string, error)

// Options controls the login window.
type Options struct {
	Port       int  // 0 = random free port
	OpenWindow bool // open an app-style browser window automatically
	Preview    bool // test mode: fake codes, no WhatsApp connection
	CloseAfter time.Duration
	Out        io.Writer // where the preview prints its URL; nil = nowhere
	// PublicURL serves the page behind a TLS proxy at this address (see
	// config.LoginPublic); Listen is then the address to bind and no window opens.
	PublicURL string
	Listen    string
	// Accounts lists the accounts of the config when there are several; the
	// window links Account. OpenAccount returns a one-time link to the login
	// window of another account (see accounts.go).
	Account     string
	Accounts    []string
	OpenAccount func(ctx context.Context, name string) (string, error)
}

// Window serves the login page and pushes state changes to it.
type Window struct {
	opts Options

	mu       sync.Mutex
	st       state
	deadline time.Time
	subs     map[chan state]struct{}
	pair     PairFunc

	auth   auth
	host   string
	bound  string // address the server listens on
	base   string // path prefix of the page, "/" unless PublicURL has a path
	secure bool   // PublicURL is https: cookies get the Secure flag
	url    string
	srv    *http.Server
	reload chan struct{}

	browser    *exec.Cmd
	profileDir string
	closed     chan struct{}
	closeOnce  sync.Once
}

// New prepares a window; call Start to serve it.
func New(opts Options) *Window {
	if opts.CloseAfter == 0 {
		opts.CloseAfter = 3 * time.Second
	}
	return &Window{
		opts:   opts,
		st:     state{Phase: "starting"},
		subs:   map[chan state]struct{}{},
		reload: make(chan struct{}, 1),
		closed: make(chan struct{}),
	}
}

// SetPair installs the phone-number pairing hook.
func (w *Window) SetPair(fn PairFunc) {
	w.mu.Lock()
	w.pair = fn
	w.mu.Unlock()
}

// URL is the page address with its one-time token ("" before Start). The
// first visit exchanges the token for a session cookie; later visits with
// the same URL are refused.
func (w *Window) URL() string { return w.url }

func (w *Window) link(token string) string {
	if w.opts.PublicURL != "" {
		return w.opts.PublicURL + "?t=" + token
	}
	return fmt.Sprintf("http://%s/?t=%s", w.host, token)
}

// NewLink issues another one-time link to this window (for example for a
// user who could not see the first one).
func (w *Window) NewLink() (string, error) {
	if w.host == "" {
		return "", fmt.Errorf("login window not started")
	}
	tok, err := w.auth.issue()
	if err != nil {
		return "", err
	}
	return w.link(tok), nil
}

// ReloadRequests fires when the user asks for a new QR code.
func (w *Window) ReloadRequests() <-chan struct{} { return w.reload }

// Done is closed when the window has been closed.
func (w *Window) Done() <-chan struct{} { return w.closed }

// Start binds the local server and, if enabled, opens the window.
func (w *Window) Start() error {
	token, err := w.auth.init()
	if err != nil {
		return err
	}
	listen, publicHost := fmt.Sprintf("127.0.0.1:%d", w.opts.Port), ""
	w.base = "/"
	if w.opts.PublicURL != "" {
		u, err := url.Parse(w.opts.PublicURL)
		if err != nil || u.Host == "" {
			return fmt.Errorf("login window: bad public URL")
		}
		listen, publicHost, w.base, w.secure = w.opts.Listen, u.Host, u.Path, u.Scheme == "https"
		if !strings.HasSuffix(w.base, "/") {
			w.base += "/"
		}
	}
	ln, err := net.Listen("tcp", listen)
	if err != nil {
		return fmt.Errorf("login window: %v", err)
	}
	host := ln.Addr().String()
	w.bound = host
	if _, port, err := net.SplitHostPort(host); err == nil {
		w.auth.cookie = sessionCookie + "_" + port
	}
	if publicHost != "" {
		host = publicHost
	}
	w.host = host
	w.url = w.link(token)

	mux := http.NewServeMux()
	mux.HandleFunc("/", w.handlePage)
	mux.HandleFunc("/events", w.handleEvents)
	mux.HandleFunc("/reload", w.handleReload)
	mux.HandleFunc("/pair", w.handlePair)
	mux.HandleFunc("/account", w.handleAccount)
	if w.opts.Preview {
		mux.HandleFunc("/preview/", w.handlePreview)
	}
	handler := w.guard(host, mux)
	if w.base != "/" {
		handler = http.StripPrefix(strings.TrimSuffix(w.base, "/"), handler)
	}
	w.srv = &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	go w.srv.Serve(ln)

	if w.opts.OpenWindow && publicHost == "" {
		w.browser, w.profileDir = openAppWindow(w.url, 440, 720)
	}
	return nil
}

func (w *Window) handlePage(rw http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(rw, r)
		return
	}
	page, err := assets.ReadFile("web/login.html")
	if err != nil {
		http.Error(rw, "missing page", http.StatusInternalServerError)
		return
	}
	csrf, _ := w.auth.session(r)
	html := strings.ReplaceAll(string(page), "{{TOKEN}}", csrf)
	html = strings.ReplaceAll(html, "{{ACCOUNTS}}", w.accountsJSON())
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
	rw.Write([]byte(html))
}

func (w *Window) handleEvents(rw http.ResponseWriter, r *http.Request) {
	fl, ok := rw.(http.Flusher)
	if !ok {
		http.Error(rw, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	rw.Header().Set("Content-Type", "text/event-stream")
	ch := make(chan state, 1)
	w.mu.Lock()
	w.subs[ch] = struct{}{}
	first := w.snapshotLocked()
	w.mu.Unlock()
	defer func() {
		w.mu.Lock()
		delete(w.subs, ch)
		w.mu.Unlock()
	}()
	send := func(s state) {
		b, _ := json.Marshal(s)
		fmt.Fprintf(rw, "data: %s\n\n", b)
		fl.Flush()
	}
	send(first)
	ping := time.NewTicker(15 * time.Second)
	defer ping.Stop()
	for {
		select {
		case s := <-ch:
			send(s)
		case <-ping.C:
			fmt.Fprint(rw, ": ping\n\n")
			fl.Flush()
		case <-r.Context().Done():
			return
		case <-w.closed:
			return
		}
	}
}

func (w *Window) handleReload(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.mu.Lock()
	fresh := w.st.Phase == "qr" && time.Until(w.deadline) > 5*time.Second
	w.mu.Unlock()
	if fresh {
		w.broadcast()
	} else {
		select {
		case w.reload <- struct{}{}:
		default:
		}
	}
	writeJSON(rw, http.StatusOK, map[string]any{"ok": true})
}

func (w *Window) handlePair(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Phone string `json:"phone"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(rw, r.Body, 1024)).Decode(&req); err != nil {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"ok": false, "error": "bad request"})
		return
	}
	phone := common.Digits(req.Phone)
	if len(phone) < 7 || len(phone) > 15 {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid_phone"})
		return
	}
	w.mu.Lock()
	pair := w.pair
	w.mu.Unlock()
	if pair == nil {
		writeJSON(rw, http.StatusServiceUnavailable, map[string]any{"ok": false, "error": "not available"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()
	code, err := pair(ctx, phone)
	if err != nil {
		writeJSON(rw, http.StatusBadGateway, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	w.mu.Lock()
	w.st = state{Phase: "code", Seq: w.st.Seq, PairCode: code, Phone: common.MaskNumber(phone)}
	w.mu.Unlock()
	w.broadcast()
	writeJSON(rw, http.StatusOK, map[string]any{"ok": true, "code": code})
}

// ShowQR publishes a new QR code valid for ttl.
func (w *Window) ShowQR(code string, ttl time.Duration) {
	w.mu.Lock()
	w.st = state{Phase: "qr", Seq: w.st.Seq + 1, SVG: qrSVG(code), Total: ttl.Milliseconds()}
	w.deadline = time.Now().Add(ttl)
	w.mu.Unlock()
	w.broadcast()
}

// Expired marks the code stream as finished; the page offers a reload button.
func (w *Window) Expired() { w.setPhase("expired", "") }

// Starting shows the loading state while a new code is being requested.
func (w *Window) Starting() { w.setPhase("starting", "") }

// Fail shows an error with a "show a new code" button.
func (w *Window) Fail(msg string) { w.setPhase("error", msg) }

// Connected shows the success screen and closes the window shortly after.
func (w *Window) Connected(phone string) {
	w.mu.Lock()
	w.st = state{Phase: "connected", Seq: w.st.Seq, Phone: common.MaskNumber(phone)}
	w.mu.Unlock()
	w.broadcast()
	if w.opts.Preview {
		return
	}
	go func() {
		time.Sleep(w.opts.CloseAfter + 700*time.Millisecond)
		w.Close()
	}()
}

// Close stops the server and the window it opened.
func (w *Window) Close() {
	w.closeOnce.Do(func() {
		close(w.closed)
		if w.browser != nil && w.browser.Process != nil {
			w.browser.Process.Kill()
			w.browser.Wait()
		}
		if w.srv != nil {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			w.srv.Shutdown(ctx)
			cancel()
		}
		if w.profileDir != "" {
			dir := w.profileDir
			go func() {
				for i := 0; i < 10; i++ {
					if os.RemoveAll(dir) == nil {
						return
					}
					time.Sleep(time.Second)
				}
			}()
		}
	})
}

func (w *Window) setPhase(phase, msg string) {
	w.mu.Lock()
	w.st = state{Phase: phase, Seq: w.st.Seq, Error: msg}
	w.mu.Unlock()
	w.broadcast()
}

func (w *Window) snapshotLocked() state {
	s := w.st
	if s.Phase == "qr" {
		s.ExpiresIn = time.Until(w.deadline).Milliseconds()
		if s.ExpiresIn < 0 {
			s.ExpiresIn = 0
		}
	}
	return s
}

func (w *Window) broadcast() {
	w.mu.Lock()
	s := w.snapshotLocked()
	for ch := range w.subs {
		select {
		case <-ch:
		default:
		}
		select {
		case ch <- s:
		default:
		}
	}
	w.mu.Unlock()
}

// qrSVG renders a QR code as a compact SVG path (no external libraries or requests).
func qrSVG(text string) string {
	q, err := qrcode.New(text, qrcode.High)
	if err != nil {
		return ""
	}
	q.DisableBorder = true
	bm := q.Bitmap()
	n := len(bm)
	var b strings.Builder
	fmt.Fprintf(&b, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges" data-modules="%d"><path fill="currentColor" d="`, n, n, n)
	for y, row := range bm {
		for x := 0; x < n; {
			if !row[x] {
				x++
				continue
			}
			start := x
			for x < n && row[x] {
				x++
			}
			fmt.Fprintf(&b, "M%d %dh%dv1h-%dz", start, y, x-start, x-start)
		}
	}
	b.WriteString(`"/></svg>`)
	return b.String()
}

func writeJSON(rw http.ResponseWriter, status int, v any) {
	rw.Header().Set("Content-Type", "application/json")
	rw.WriteHeader(status)
	json.NewEncoder(rw).Encode(v)
}
