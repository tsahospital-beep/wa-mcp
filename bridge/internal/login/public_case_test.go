package login

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestPublicHostIgnoresCaseAndFooterSaysServer(t *testing.T) {
	w := New(Options{Preview: true, PublicURL: "http://localhost:8443/login/", Listen: "127.0.0.1:0"})
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Close)
	c := publicClient(w)
	link := strings.Replace(w.URL(), "localhost", "LocalHost", 1)
	resp, err := c.Get(link)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusSeeOther {
		t.Fatalf("mixed-case host refused: %d", resp.StatusCode)
	}
	req, _ := http.NewRequest("GET", "http://LOCALHOST:8443/login/", nil)
	for _, ck := range resp.Cookies() {
		req.AddCookie(ck)
	}
	page, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(page.Body)
	page.Body.Close()
	if page.StatusCode != 200 || !strings.Contains(string(body), "const PUBLIC = true;") {
		t.Fatalf("page %d, public flag missing", page.StatusCode)
	}
}
