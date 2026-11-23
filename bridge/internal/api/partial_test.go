package api

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

type failSecond struct {
	*fakeTransport
	calls int
}

func (f *failSecond) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	f.calls++
	if f.calls == 2 {
		return "", time.Time{}, errors.New("network down")
	}
	id, ts, err := f.fakeTransport.SendMessage(ctx, to, msg)
	return id + string(rune('0'+f.calls)), ts, err
}

func TestPartialSendReturnsTheSentIDs(t *testing.T) {
	e := newEnv(t, true)
	e.snd.Transport = &failSecond{fakeTransport: e.tr}
	sticker := filepath.Join(e.cfg.Sending.AllowedMediaDirs[0], "wave.webp")
	os.WriteFile(sticker, []byte("RIFF0000WEBPVP8 "), 0o600)
	body := map[string]any{"to": "15550100002", "text": "hello", "sticker_path": sticker, "human": false}
	code, out := e.do(t, "POST", "/api/v1/send", body)
	ids, _ := out["ids"].([]any)
	if code < 400 || out["partial"] != true || len(ids) != 1 || ids[0] != "OUT11" {
		t.Fatalf("partial failure: %d %v", code, out)
	}
	code, out = e.do(t, "POST", "/api/v1/send", body)
	ids, _ = out["ids"].([]any)
	if code != 200 || out["resumed"] != true || len(ids) != 2 || ids[0] != "OUT11" {
		t.Fatalf("resumed: %d %v", code, out)
	}
}
