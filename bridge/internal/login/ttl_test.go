package login

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestLoginSecretsExpire(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	a := &auth{now: func() time.Time { return now }}
	first, err := a.init()
	if err != nil {
		t.Fatal(err)
	}
	var later []string
	for i := 0; i < MaxOpenTokens; i++ {
		now = now.Add(time.Second)
		tok, _ := a.issue()
		later = append(later, tok)
	}
	if len(a.tokens) != MaxOpenTokens {
		t.Fatalf("%d open tokens", len(a.tokens))
	}
	if a.exchange(httptest.NewRecorder(), first, "/", false) {
		t.Fatal("the oldest token still worked after more than MaxOpenTokens were issued")
	}
	now = now.Add(TokenTTL)
	if a.exchange(httptest.NewRecorder(), later[0], "/", false) {
		t.Fatal("an expired token worked")
	}
	fresh, _ := a.issue()
	rec := httptest.NewRecorder()
	if !a.exchange(rec, fresh, "/", false) {
		t.Fatal("a fresh token was refused")
	}
	cookie := rec.Result().Cookies()[0]
	if cookie.MaxAge != int(SessionTTL/time.Second) {
		t.Fatalf("cookie max age %d", cookie.MaxAge)
	}
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.AddCookie(&http.Cookie{Name: cookie.Name, Value: cookie.Value})
	if _, ok := a.session(req); !ok {
		t.Fatal("a new session was refused")
	}
	now = now.Add(SessionTTL)
	if _, ok := a.session(req); ok {
		t.Fatal("an expired session worked")
	}
}
