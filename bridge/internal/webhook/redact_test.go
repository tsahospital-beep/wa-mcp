package webhook

import (
	"bytes"
	"log"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

func TestFailureLogHasNoSecretURL(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close() // nothing answers there
	var buf bytes.Buffer
	done := make(chan bool, 1)
	hook := "http://user:pass@" + addr + "/hook?key=topsecret"
	d, _ := New([]config.Webhook{{URL: hook}}, func(string) string { return "" },
		Options{Log: log.New(&buf, "", 0), Delays: []time.Duration{}, Done: func(string, bool) { done <- true }})
	ctx := t.Context()
	d.Start(ctx)
	d.Emit("message", map[string]string{"x": "y"})
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		t.Fatal("no delivery result")
	}
	for _, secret := range []string{"topsecret", "pass@", "user:"} {
		if strings.Contains(buf.String(), secret) {
			t.Fatalf("the log shows %q: %s", secret, buf.String())
		}
	}
	if !strings.Contains(buf.String(), "failed after 1 attempts") {
		t.Fatalf("log: %s", buf.String())
	}
}
