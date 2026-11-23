package login

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// RunPreview serves the login window with fake codes and never touches WhatsApp.
// Code lifetimes follow the real protocol (60 s, then 20 s) multiplied by scale.
// It returns when ctx ends.
func RunPreview(ctx context.Context, opts Options, scale float64) error {
	if scale <= 0 {
		scale = 1
	}
	opts.Preview = true
	w := New(opts)
	w.SetPair(func(ctx context.Context, phone string) (string, error) {
		select {
		case <-time.After(900 * time.Millisecond):
		case <-ctx.Done():
			return "", ctx.Err()
		}
		return "K7PM-2QXD", nil
	})
	if err := w.Start(); err != nil {
		return err
	}
	defer w.Close()
	if opts.Out != nil {
		fmt.Fprintf(opts.Out, "Login window: %s\n", w.URL())
		fmt.Fprintln(opts.Out, "Test mode: fake QR codes, no WhatsApp connection. Ctrl+C to quit.")
	}
	stream := func(stop <-chan struct{}) {
		select {
		case <-time.After(1200 * time.Millisecond):
		case <-stop:
			return
		}
		for i := 0; i < 6; i++ {
			d := 20 * time.Second
			if i == 0 {
				d = 60 * time.Second
			}
			d = time.Duration(float64(d) * scale)
			w.ShowQR(fakeQRPayload(), d)
			select {
			case <-stop:
				return
			case <-time.After(d):
			}
		}
		w.Expired()
	}
	stop := make(chan struct{})
	go stream(stop)
	for {
		select {
		case <-ctx.Done():
			close(stop)
			return nil
		case <-w.ReloadRequests():
			close(stop)
			stop = make(chan struct{})
			w.Starting()
			go stream(stop)
		}
	}
}

// handlePreview lets screenshots and tests jump to any state.
func (w *Window) handlePreview(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	switch strings.TrimPrefix(r.URL.Path, "/preview/") {
	case "success":
		w.Connected("15550104567")
	case "expire":
		w.Expired()
	case "error":
		w.Fail("The phone did not confirm the link in time.")
	case "qr":
		w.ShowQR(fakeQRPayload(), 20*time.Second)
	default:
		http.NotFound(rw, r)
		return
	}
	writeJSON(rw, http.StatusOK, map[string]any{"ok": true})
}

func fakeQRPayload() string {
	part := func(n int) string {
		b := make([]byte, n)
		rand.Read(b)
		return base64.StdEncoding.EncodeToString(b)
	}
	return "2@" + part(48) + "," + part(32) + "," + part(32) + "," + part(32) + ",1"
}
