package sender

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"go.mau.fi/whatsmeow"
)

type growingUpload struct {
	*fakeTransport
	path string
}

func (u growingUpload) Upload(ctx context.Context, r io.Reader, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
	f, err := os.OpenFile(u.path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return whatsmeow.UploadResponse{}, err
	}
	if _, err = f.WriteString("new data"); err != nil {
		f.Close()
		return whatsmeow.UploadResponse{}, err
	}
	if err = f.Close(); err != nil {
		return whatsmeow.UploadResponse{}, err
	}
	return u.fakeTransport.Upload(ctx, r, kind)
}

func TestUploadStopsAtCheckedSize(t *testing.T) {
	r := newRig(t, nil)
	path := filepath.Join(t.TempDir(), "notes.txt")
	if err := os.WriteFile(path, []byte("original"), 0600); err != nil {
		t.Fatal(err)
	}
	r.s.Transport = growingUpload{r.tr, path}
	_, err := r.s.Send(bg, Request{To: bob, MediaPath: path, Human: noHuman()})
	if err != nil {
		t.Fatal(err)
	}
	if n := r.tr.sent[0].msg.GetDocumentMessage().GetFileLength(); n != 8 {
		t.Fatalf("sent %d bytes", n)
	}
}

func TestAsDocumentSendsAnyFileAsDocument(t *testing.T) {
	r := newRig(t, nil)
	dir := t.TempDir()
	song := filepath.Join(dir, "demo.mp3")
	os.WriteFile(song, []byte("ID3 fake"), 0o600)
	res, err := r.s.Send(bg, Request{To: bob, Text: "final mix", MediaPath: song, AsDocument: true, Human: noHuman()})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.tr.sent) != 1 {
		t.Fatalf("want one message (document with caption), got %d", len(r.tr.sent))
	}
	doc := r.tr.sent[0].msg.GetDocumentMessage()
	if doc == nil || doc.GetFileName() != "demo.mp3" || doc.GetCaption() != "final mix" || doc.GetMimetype() != "audio/mpeg" {
		t.Fatalf("not sent as document: %v", r.tr.sent[0].msg)
	}
	m, err := r.db.GetMessage(bg, bob, res.ID)
	if err != nil || m.Kind != store.KindDocument || m.AttachmentName != "demo.mp3" {
		t.Fatalf("stored %+v, %v", m, err)
	}

	// Without the option the same file is an audio message after its text.
	r.s.Cfg.DedupeWindowMinutes = 0
	if _, err := r.s.Send(bg, Request{To: bob, Text: "final mix", MediaPath: song, Human: noHuman()}); err != nil {
		t.Fatal(err)
	}
	if len(r.tr.sent) != 3 || r.tr.sent[2].msg.GetAudioMessage() == nil {
		t.Fatalf("default kind changed: %d messages", len(r.tr.sent))
	}
}

func TestAsDocumentSurvivesQuietHoursQueue(t *testing.T) {
	r := newRig(t, func(c *config.Sending) {
		c.QuietHours = config.QuietHours{Enabled: true, Start: "11:00", End: "13:00", Action: "queue"}
	})
	song := filepath.Join(t.TempDir(), "demo.wav")
	os.WriteFile(song, []byte("RIFF fake"), 0o600)
	res, err := r.s.Send(bg, Request{To: bob, MediaPath: song, AsDocument: true})
	if err != nil || !res.Queued {
		t.Fatalf("want queued, got %+v %v", res, err)
	}
	sc, err := r.db.GetScheduled(bg, res.ScheduledID)
	if err != nil || sc.Kind != store.KindDocument {
		t.Fatalf("queued kind: %+v %v", sc, err)
	}
	req := RequestFromSchedule(sc)
	if !req.AsDocument || req.MediaPath != fsutil.Resolve(song) {
		t.Fatalf("rebuilt request lost the document flag: %+v", req)
	}
	r.clock.Advance(2 * time.Hour)
}
