package login

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

// publicClient sends every request to the window's listener while the URL
// (and so the Host header) names the public address, as a proxy would.
func publicClient(w *Window) *http.Client {
	dial := &net.Dialer{Timeout: 5 * time.Second}
	return &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return dial.DialContext(ctx, network, w.bound)
		}},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func TestPublicWindowBehindProxy(t *testing.T) {
	w := New(Options{Preview: true, OpenWindow: true, PublicURL: "http://localhost:8443/login/", Listen: "127.0.0.1:0"})
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Close)
	if w.browser != nil {
		t.Fatal("a public login page must not open a local window")
	}
	if !strings.HasPrefix(w.URL(), "http://localhost:8443/login/?t=") {
		t.Fatalf("link: %s", w.URL())
	}
	c := publicClient(w)

	resp, err := c.Get(w.URL())
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	cookie := resp.Header.Get("Set-Cookie")
	if resp.StatusCode != http.StatusSeeOther || resp.Header.Get("Location") != "/login/" {
		t.Fatalf("exchange: %d %q", resp.StatusCode, resp.Header.Get("Location"))
	}
	if !strings.Contains(cookie, "Path=/login/") || !strings.Contains(cookie, "HttpOnly") {
		t.Fatalf("cookie: %q", cookie)
	}

	// The token is spent.
	if resp, err = c.Get(w.URL()); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("second use: %d", resp.StatusCode)
	}

	// The page under the prefix works with the cookie and uses relative request paths.
	req, _ := http.NewRequest("GET", "http://localhost:8443/login/", nil)
	req.Header.Set("Cookie", strings.SplitN(cookie, ";", 2)[0])
	if resp, err = c.Do(req); err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || !strings.Contains(string(body), `new EventSource("events")`) {
		t.Fatalf("page: %d", resp.StatusCode)
	}

	// Another Host (the bare listener address) is refused, and paths outside the prefix are not served.
	direct := &http.Client{Timeout: 5 * time.Second}
	if resp, err = direct.Get("http://" + w.bound + "/login/"); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("direct host: %d", resp.StatusCode)
	}
	req, _ = http.NewRequest("GET", "http://localhost:8443/events", nil)
	req.Header.Set("Cookie", strings.SplitN(cookie, ";", 2)[0])
	if resp, err = c.Do(req); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("outside the prefix: %d", resp.StatusCode)
	}
}

func TestPublicWindowSecureCookie(t *testing.T) {
	w := New(Options{Preview: true, PublicURL: "https://wa.example.test/login/", Listen: "127.0.0.1:0"})
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Close)
	u, _ := url.Parse(w.URL())
	req, _ := http.NewRequest("GET", "http://"+w.bound+"/login/?"+u.RawQuery, nil)
	req.Host = "wa.example.test"
	resp, err := publicClient(w).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if c := resp.Header.Get("Set-Cookie"); resp.StatusCode != http.StatusSeeOther || !strings.Contains(c, "Secure") {
		t.Fatalf("https public URL: %d %q", resp.StatusCode, c)
	}
}
