package login

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strings"
	"testing"
	"time"
)

func startPreview(t *testing.T) *Window {
	t.Helper()
	w := New(Options{Preview: true})
	w.SetPair(func(ctx context.Context, phone string) (string, error) { return "ABCD-EFGH", nil })
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Close)
	return w
}

func newClient(t *testing.T) *http.Client {
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	return &http.Client{Jar: jar, Timeout: 10 * time.Second}
}

var tokenInPage = regexp.MustCompile(`const TOKEN = "([0-9a-f]+)"`)

// open exchanges the one-time token and returns a logged-in client and the header token.
func open(t *testing.T, w *Window) (*http.Client, string, string) {
	t.Helper()
	c := newClient(t)
	resp, err := c.Get(w.URL())
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("exchange: status %d", resp.StatusCode)
	}
	m := tokenInPage.FindStringSubmatch(string(body))
	if m == nil {
		t.Fatal("page lacks the header token")
	}
	u, _ := url.Parse(w.URL())
	return c, m[1], "http://" + u.Host
}

func TestOneTimeTokenExchange(t *testing.T) {
	w := startPreview(t)
	u, _ := url.Parse(w.URL())
	oneTime := u.Query().Get("t")

	// The exchange answers with a cookie and a redirect, not the page.
	plain := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := plain.Get(w.URL())
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusSeeOther || resp.Header.Get("Location") != "/" {
		t.Fatalf("exchange: %d %q", resp.StatusCode, resp.Header.Get("Location"))
	}
	cookie := resp.Header.Get("Set-Cookie")
	if !strings.Contains(cookie, sessionCookie+"_"+u.Port()+"=") || !strings.Contains(cookie, "HttpOnly") || !strings.Contains(cookie, "SameSite=Strict") {
		t.Fatalf("cookie attributes: %q", cookie)
	}

	// The token is spent: a second visit is refused.
	resp, err = newClient(t).Get(w.URL())
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("second use: status %d", resp.StatusCode)
	}

	// The session cookie from the first visit works and the page does not contain the one-time token.
	req, _ := http.NewRequest("GET", "http://"+u.Host+"/", nil)
	req.Header.Set("Cookie", strings.SplitN(cookie, ";", 2)[0])
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || strings.Contains(string(body), oneTime) || strings.Contains(string(body), "{{TOKEN}}") {
		t.Fatalf("page with cookie: status %d", resp.StatusCode)
	}
	if resp.Header.Get("Content-Security-Policy") == "" {
		t.Fatal("CSP missing")
	}
}

func TestRequestsNeedSession(t *testing.T) {
	w := startPreview(t)
	u, _ := url.Parse(w.URL())
	for _, path := range []string{"/", "/events", "/?t=wrong"} {
		resp, err := http.Get("http://" + u.Host + path)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("%s without session: status %d", path, resp.StatusCode)
		}
	}

	c, csrf, base := open(t, w)
	post := func(withHeader bool) int {
		req, _ := http.NewRequest("POST", base+"/reload", strings.NewReader("{}"))
		if withHeader {
			req.Header.Set("X-Login-Token", csrf)
		}
		resp, err := c.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code := post(false); code != http.StatusForbidden {
		t.Fatalf("POST without header token: %d", code)
	}
	if code := post(true); code != http.StatusOK {
		t.Fatalf("POST with session and header: %d", code)
	}
	// Header token alone (no cookie) is refused.
	req, _ := http.NewRequest("POST", base+"/reload", strings.NewReader("{}"))
	req.Header.Set("X-Login-Token", csrf)
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("POST without cookie: %d", resp.StatusCode)
	}

	// A different Host header is refused (DNS rebinding guard).
	req, _ = http.NewRequest("GET", base+"/", nil)
	req.Host = "evil.example:80"
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("foreign host: status %d", resp.StatusCode)
	}
}

func readEvent(t *testing.T, r *bufio.Reader) state {
	t.Helper()
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(line, "data: ") {
			var s state
			if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &s); err != nil {
				t.Fatal(err)
			}
			return s
		}
	}
}

func TestEventsStream(t *testing.T) {
	w := startPreview(t)
	c, csrf, base := open(t, w)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", base+"/events?t="+csrf, nil)
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("content type %q", ct)
	}
	r := bufio.NewReader(resp.Body)
	if first := readEvent(t, r); first.Phase != "starting" {
		t.Fatalf("first event: %+v", first)
	}

	w.ShowQR("2@test,code", 20*time.Second)
	s := readEvent(t, r)
	if s.Phase != "qr" || s.Seq != 1 || !strings.HasPrefix(s.SVG, "<svg") || s.ExpiresIn <= 0 || s.Total != 20000 {
		t.Fatalf("qr event: phase=%s seq=%d", s.Phase, s.Seq)
	}

	pr, _ := http.NewRequest("POST", base+"/pair", strings.NewReader(`{"phone":"+1 555 010 4567"}`))
	pr.Header.Set("X-Login-Token", csrf)
	presp, err := c.Do(pr)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	json.NewDecoder(presp.Body).Decode(&out)
	presp.Body.Close()
	if out["ok"] != true || out["code"] != "ABCD-EFGH" {
		t.Fatalf("pair: %v", out)
	}
	if s := readEvent(t, r); s.Phase != "code" || s.Phone != "+1 ••• ••• 4567" {
		t.Fatalf("code event: %+v", s)
	}

	w.Connected("15550104567")
	if s := readEvent(t, r); s.Phase != "connected" {
		t.Fatalf("connected event: %+v", s)
	}
}

func TestReloadRequest(t *testing.T) {
	w := startPreview(t)
	c, csrf, base := open(t, w)
	w.Expired()
	req, _ := http.NewRequest("POST", base+"/reload", strings.NewReader("{}"))
	req.Header.Set("X-Login-Token", csrf)
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	select {
	case <-w.ReloadRequests():
	case <-time.After(2 * time.Second):
		t.Fatal("reload request not delivered")
	}
}

func TestRunPreviewStops(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- RunPreview(ctx, Options{}, 0.01) }()
	time.Sleep(200 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("preview did not stop")
	}
}

func TestNewLinkIsSingleUseToo(t *testing.T) {
	w := startPreview(t)
	_, _, _ = open(t, w) // the first link is spent
	link, err := w.NewLink()
	if err != nil {
		t.Fatal(err)
	}
	if link == w.URL() {
		t.Fatal("a new link must carry a new token")
	}
	c := newClient(t)
	resp, err := c.Get(link)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("new link: %d", resp.StatusCode)
	}
	resp, err = newClient(t).Get(link)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 403 {
		t.Fatalf("new link used twice: %d", resp.StatusCode)
	}
}

func TestPageUsesCookieForEvents(t *testing.T) {
	page, err := assets.ReadFile("web/login.html")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(page), `new EventSource("events")`) || strings.Contains(string(page), `events?t=`) {
		t.Fatal("the event stream must not carry a token in its URL")
	}
}
