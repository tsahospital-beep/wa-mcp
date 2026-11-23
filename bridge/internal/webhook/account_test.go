package webhook

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

func TestEnvelopeNamesTheAccount(t *testing.T) {
	got := make(chan Envelope, 2)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		var env Envelope
		json.Unmarshal(b, &env)
		got <- env
		w.WriteHeader(204)
	}))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	for _, acct := range []string{"work", ""} {
		d, _ := New([]config.Webhook{{URL: srv.URL}}, func(string) string { return "" },
			Options{Account: acct, Self: func() string { return "+1 *** *** 0101" }})
		d.Start(ctx)
		d.Emit("security", map[string]string{"kind": "logged_out"})
		select {
		case env := <-got:
			want := acct
			if want == "" {
				want = config.DefaultAccount
			}
			if env.Account != want || env.Self != "+1 *** *** 0101" || env.Event != "security" {
				t.Fatalf("envelope %+v", env)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("no delivery")
		}
	}
}
