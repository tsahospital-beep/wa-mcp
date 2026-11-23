package login

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"net/http"
	"sync"
	"time"
)

// Lifetimes of the login secrets: a one-time link works for TokenTTL, at
// most MaxOpenTokens links are valid at once (a new one drops the oldest),
// and a session cookie works for SessionTTL.
const (
	TokenTTL      = 10 * time.Minute
	MaxOpenTokens = 5
	SessionTTL    = 12 * time.Hour
)

// sessionCookie holds the session id after the one-time token was exchanged.
const sessionCookie = "wa_mcp_login"

// auth implements the one-time token exchange: the first GET /?t=<token>
// sets an HttpOnly, SameSite=Strict cookie and redirects to /. The token is
// then spent. The page gets a separate value for the X-Login-Token header
// of its POST requests.
type auth struct {
	mu       sync.Mutex
	tokens   map[string]time.Time // unused one-time tokens -> issue time
	sessions map[string]session   // session id -> header token and start
	// now is the clock; nil means time.Now.
	now func() time.Time
	// cookie is sessionCookie plus the port, so the login windows of two
	// accounts on one host keep their own sessions.
	cookie string
}

type session struct {
	csrf  string
	start time.Time
}

func (a *auth) clock() time.Time {
	if a.now != nil {
		return a.now()
	}
	return time.Now()
}

// pruneLocked drops expired tokens and sessions.
func (a *auth) pruneLocked(now time.Time) {
	for t, at := range a.tokens {
		if now.Sub(at) >= TokenTTL {
			delete(a.tokens, t)
		}
	}
	for sid, s := range a.sessions {
		if now.Sub(s.start) >= SessionTTL {
			delete(a.sessions, sid)
		}
	}
}

func (a *auth) cookieName() string {
	if a.cookie != "" {
		return a.cookie
	}
	return sessionCookie
}

func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func (a *auth) init() (string, error) {
	a.mu.Lock()
	a.tokens, a.sessions = map[string]time.Time{}, map[string]session{}
	a.mu.Unlock()
	return a.issue()
}

// issue adds another one-time token.
func (a *auth) issue() (string, error) {
	tok, err := randomHex(24)
	if err != nil {
		return "", err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	now := a.clock()
	a.pruneLocked(now)
	for len(a.tokens) >= MaxOpenTokens {
		oldest := ""
		for t, at := range a.tokens {
			if oldest == "" || at.Before(a.tokens[oldest]) {
				oldest = t
			}
		}
		delete(a.tokens, oldest)
	}
	a.tokens[tok] = now
	return tok, nil
}

// exchange spends the token and starts a session; false when the token is
// wrong or already used.
func (a *auth) exchange(rw http.ResponseWriter, tok, path string, secure bool) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	now := a.clock()
	a.pruneLocked(now)
	match := ""
	for t := range a.tokens {
		if subtle.ConstantTimeCompare([]byte(tok), []byte(t)) == 1 {
			match = t
		}
	}
	if match == "" {
		return false
	}
	sid, err1 := randomHex(32)
	csrf, err2 := randomHex(24)
	if err1 != nil || err2 != nil {
		return false
	}
	delete(a.tokens, match)
	a.sessions[sid] = session{csrf: csrf, start: now}
	http.SetCookie(rw, &http.Cookie{Name: a.cookieName(), Value: sid, Path: path, HttpOnly: true, Secure: secure,
		SameSite: http.SameSiteStrictMode, MaxAge: int(SessionTTL / time.Second)})
	return true
}

// session returns the header token of the request's session.
func (a *auth) session(r *http.Request) (string, bool) {
	c, err := r.Cookie(a.cookieName())
	if err != nil || c.Value == "" {
		return "", false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.pruneLocked(a.clock())
	for sid, s := range a.sessions {
		if subtle.ConstantTimeCompare([]byte(sid), []byte(c.Value)) == 1 {
			return s.csrf, true
		}
	}
	return "", false
}

// guard rejects requests that are not addressed to our exact host (DNS
// rebinding), handles the token exchange, and requires the session cookie
// (plus the header token for POST) on everything else.
func (w *Window) guard(host string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		if r.Host != host {
			http.Error(rw, "forbidden", http.StatusForbidden)
			return
		}
		rw.Header().Set("Cache-Control", "no-store")
		rw.Header().Set("X-Content-Type-Options", "nosniff")
		rw.Header().Set("Referrer-Policy", "no-referrer")
		if r.Method == http.MethodGet && r.URL.Path == "/" && r.URL.Query().Has("t") {
			if !w.auth.exchange(rw, r.URL.Query().Get("t"), w.base, w.secure) {
				http.Error(rw, "This login link was already used or has expired. Use the window that opened, run wa-bridge login-url for a new link, or scan the QR code in the terminal.", http.StatusForbidden)
				return
			}
			http.Redirect(rw, r, w.base, http.StatusSeeOther)
			return
		}
		csrf, ok := w.auth.session(r)
		if !ok {
			http.Error(rw, "forbidden", http.StatusForbidden)
			return
		}
		if r.Method == http.MethodPost && subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Login-Token")), []byte(csrf)) != 1 {
			http.Error(rw, "forbidden", http.StatusForbidden)
			return
		}
		next.ServeHTTP(rw, r)
	})
}
