package sender

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

// flakyTransport fails the send numbers in failAt once (1-based over all calls).
type flakyTransport struct {
	fakeTransport
	calls  int
	failAt map[int]error
}

func (f *flakyTransport) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	f.calls++
	if err, ok := f.failAt[f.calls]; ok {
		return "", time.Time{}, err
	}
	return f.fakeTransport.SendMessage(ctx, to, msg)
}

func TestPartialSendIsResumed(t *testing.T) {
	r := newRig(t, nil)
	tr := &flakyTransport{fakeTransport: fakeTransport{clock: r.clock}, failAt: map[int]error{2: errors.New("network down")}}
	r.s.Transport = tr
	photo := filepath.Join(t.TempDir(), "photo.jpg")
	os.WriteFile(photo, []byte("\xff\xd8\xff\xe0fakejpeg"), 0o600)
	sticker := filepath.Join(t.TempDir(), "wave.webp")
	os.WriteFile(sticker, []byte("RIFF0000WEBPVP8 "), 0o600)
	req := Request{To: bob, Text: "the cover", MediaPath: photo, StickerPath: sticker, Human: noHuman()}

	res, err := r.s.Send(bg, req)
	if err == nil || len(res.IDs) != 1 || res.IDs[0] != "OUTA" {
		t.Fatalf("first attempt: %+v %v", res, err)
	}
	res, err = r.s.Send(bg, req)
	if err != nil || !res.Resumed || len(res.IDs) != 2 || res.IDs[0] != "OUTA" || res.IDs[1] != "OUTB" {
		t.Fatalf("resume: %+v %v", res, err)
	}
	if len(tr.sent) != 2 {
		t.Fatalf("%d messages went out, want 2 (photo with caption, sticker)", len(tr.sent))
	}
	if _, err := r.s.Send(bg, req); !errors.Is(err, ErrDuplicate) {
		t.Fatalf("a complete send must still be a duplicate: %v", err)
	}
	// After the dedupe window a complete send may go out again.
	r.clock.Advance(11 * time.Minute)
	if res, err := r.s.Send(bg, req); err != nil || res.Resumed || len(tr.sent) != 4 {
		t.Fatalf("new send: %+v %v (%d sent)", res, err, len(tr.sent))
	}
}

func TestUnknownPartIsNotSentTwice(t *testing.T) {
	r := newRig(t, nil)
	tr := &flakyTransport{fakeTransport: fakeTransport{clock: r.clock}, failAt: map[int]error{1: context.DeadlineExceeded}}
	r.s.Transport = tr
	sticker := filepath.Join(t.TempDir(), "wave.webp")
	os.WriteFile(sticker, []byte("RIFF0000WEBPVP8 "), 0o600)
	req := Request{To: bob, Text: "hello", StickerPath: sticker, Human: noHuman()}
	if _, err := r.s.Send(bg, req); err == nil {
		t.Fatal("the lost answer was not reported")
	}
	res, err := r.s.Send(bg, req)
	if err != nil || !res.Resumed || len(tr.sent) != 1 || tr.sent[0].msg.GetStickerMessage() == nil {
		t.Fatalf("retry: %+v %v, sent %d", res, err, len(tr.sent))
	}
}

func TestChatLockWaitHonorsCancellation(t *testing.T) {
	s := &Sender{}
	release, err := s.lockChat(bg, bob)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	ctx, cancel := context.WithCancel(bg)
	cancel()
	if _, err := s.lockChat(ctx, bob); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestIdleChatStateExpiresWithoutSplittingActiveLock(t *testing.T) {
	r := newRig(t, nil)
	if _, err := r.s.Send(bg, Request{To: bob, Text: "hello", Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	hold, _ := r.s.lockChat(bg, "active")
	defer hold()
	r.clock.Advance(2 * time.Minute)
	release, _ := r.s.lockChat(bg, "another")
	release()
	if len(r.s.chats) != 1 || r.s.chats["active"] == nil {
		t.Fatal("idle chats retained or active lock lost")
	}
}

func TestNewPartialSendIgnoresOlderCompleteSend(t *testing.T) {
	r := newRig(t, nil)
	tr := &flakyTransport{fakeTransport: fakeTransport{clock: r.clock}, failAt: map[int]error{4: errors.New("offline")}}
	r.s.Transport = tr
	sticker := filepath.Join(t.TempDir(), "wave.webp")
	if err := os.WriteFile(sticker, []byte("RIFF0000WEBPVP8 "), 0o600); err != nil {
		t.Fatal(err)
	}
	req := Request{To: bob, Text: "hello", StickerPath: sticker, Human: noHuman()}
	if _, err := r.s.Send(bg, req); err != nil {
		t.Fatal(err)
	}
	r.clock.Advance(11 * time.Minute)
	if res, err := r.s.Send(bg, req); err == nil || len(res.IDs) != 1 {
		t.Fatalf("partial: %+v %v", res, err)
	}
	res, err := r.s.Send(bg, req)
	if err != nil || !res.Resumed || len(res.IDs) != 2 || res.IDs[0] != "OUTC" || res.IDs[1] != "OUTD" {
		t.Fatalf("retry: %+v %v", res, err)
	}
}
