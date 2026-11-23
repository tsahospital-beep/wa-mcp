package login

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
)

func startWithAccounts(t *testing.T, open func(ctx context.Context, name string) (string, error)) *Window {
	t.Helper()
	w := New(Options{Preview: true, Account: "work", Accounts: []string{"default", "work", "club"}, OpenAccount: open})
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Close)
	return w
}

func postAccount(t *testing.T, c *http.Client, base, csrf, name string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest("POST", base+"/account", strings.NewReader(fmt.Sprintf(`{"name":%q}`, name)))
	if csrf != "" {
		req.Header.Set("X-Login-Token", csrf)
	}
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestAccountSelector(t *testing.T) {
	var mu sync.Mutex
	var asked []string
	w := startWithAccounts(t, func(ctx context.Context, name string) (string, error) {
		mu.Lock()
		asked = append(asked, name)
		mu.Unlock()
		switch name {
		case "default":
			return "http://127.0.0.1:5000/?t=abc", nil
		case "club":
			return "", fmt.Errorf("%w: no login in progress", ErrLinked)
		}
		return "", errors.New("unexpected")
	})
	c, csrf, base := open(t, w)

	resp, err := c.Get(base + "/")
	if err != nil {
		t.Fatal(err)
	}
	page, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !strings.Contains(string(page), `const ACCOUNTS = {"current":"work","names":["default","work","club"]};`) ||
		strings.Contains(string(page), "{{ACCOUNTS}}") {
		t.Fatal("page lacks the account list")
	}

	if code, out := postAccount(t, c, base, csrf, "default"); code != 200 || out["url"] != "http://127.0.0.1:5000/?t=abc" {
		t.Fatalf("open default: %d %v", code, out)
	}
	if code, out := postAccount(t, c, base, csrf, "club"); code != http.StatusConflict || out["error"] != "linked" {
		t.Fatalf("linked account: %d %v", code, out)
	}
	for _, name := range []string{"work", "nobody", ""} {
		if code, out := postAccount(t, c, base, csrf, name); code != http.StatusNotFound || out["error"] != "unknown_account" {
			t.Fatalf("%q: %d %v", name, code, out)
		}
	}
	if code, _ := postAccount(t, c, base, "", "default"); code != http.StatusForbidden {
		t.Fatalf("without the header token: %d", code)
	}
	if strings.Join(asked, ",") != "default,club" {
		t.Fatalf("asked for %v; the window must only ask for listed other accounts", asked)
	}
}

func TestAccountSelectorStopped(t *testing.T) {
	w := startWithAccounts(t, func(ctx context.Context, name string) (string, error) {
		return "", fmt.Errorf("%w on 127.0.0.1:8081: connection refused", ErrNotRunning)
	})
	c, csrf, base := open(t, w)
	if code, out := postAccount(t, c, base, csrf, "default"); code != http.StatusServiceUnavailable || out["error"] != "not_running" {
		t.Fatalf("stopped bridge: %d %v", code, out)
	}
}

func TestOneAccountShowsNoSelector(t *testing.T) {
	w := startPreview(t)
	c, csrf, base := open(t, w)
	resp, err := c.Get(base + "/")
	if err != nil {
		t.Fatal(err)
	}
	page, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !strings.Contains(string(page), `const ACCOUNTS = {"current":"","names":[]};`) {
		t.Fatal("a single account must get an empty list")
	}
	if code, _ := postAccount(t, c, base, csrf, "default"); code != http.StatusNotFound {
		t.Fatalf("selector without accounts: %d", code)
	}
}

// The windows of two accounts share 127.0.0.1, so their cookies must not overwrite each other.
func TestTwoWindowsKeepTheirSessions(t *testing.T) {
	first, second := startPreview(t), startPreview(t)
	c := newClient(t)
	csrfs := map[*Window]string{}
	for _, w := range []*Window{first, second} {
		resp, err := c.Get(w.URL())
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		m := tokenInPage.FindStringSubmatch(string(body))
		if resp.StatusCode != 200 || m == nil {
			t.Fatalf("exchange: %d", resp.StatusCode)
		}
		csrfs[w] = m[1]
	}
	for _, w := range []*Window{first, second} {
		u, _ := url.Parse(w.URL())
		req, _ := http.NewRequest("POST", "http://"+u.Host+"/reload", strings.NewReader("{}"))
		req.Header.Set("X-Login-Token", csrfs[w])
		resp, err := c.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatalf("window on %s lost its session: %d", u.Host, resp.StatusCode)
		}
	}
}
