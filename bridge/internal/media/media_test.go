package media

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

type fakeSource struct {
	expiredPaths map[string]bool
	reuploaded   int
	downloads    []string
	reuploadErr  error
	size         int // bytes to write; 0 = a short text
	written      int // bytes accepted by the file
}

func (f *fakeSource) DownloadMediaToFile(ctx context.Context, path string, enc, plain, key []byte,
	kind whatsmeow.MediaType, file whatsmeow.File) error {
	f.downloads = append(f.downloads, path)
	if f.expiredPaths[path] {
		file.Write([]byte("partial"))
		return fmt.Errorf("download: %w", whatsmeow.ErrMediaDownloadFailedWith410)
	}
	if f.size > 0 {
		// Stream in 1 KiB chunks like a network download.
		for f.written < f.size {
			n, err := file.Write(make([]byte, 1024))
			f.written += n
			if err != nil {
				return err
			}
		}
		return nil
	}
	_, err := file.Write([]byte("data from " + path))
	return err
}

func (f *fakeSource) RequestReupload(ctx context.Context, m *store.Message) (string, error) {
	f.reuploaded++
	if f.reuploadErr != nil {
		return "", f.reuploadErr
	}
	return "/v/fresh/" + m.ID + ".enc?oh=new", nil
}

func setup(t *testing.T) (*store.DB, string) {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db, filepath.Join(dir, "downloads")
}

func noPartFiles(t *testing.T, dir string) {
	t.Helper()
	filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err == nil && strings.HasPrefix(d.Name(), ".part-") {
			t.Errorf("temporary file left: %s", p)
		}
		return nil
	})
}

func TestFetchWithReupload(t *testing.T) {
	db, dir := setup(t)
	ctx := context.Background()
	conv := "120363000000000001@g.us"
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "IMG1", Author: "15550100003@s.whatsapp.net", SentAt: 10,
		Kind: "image", Mime: "image/jpeg", DirectPath: "/v/old.enc?oh=1", MediaKey: []byte{1}, EncSHA256: []byte{2}, PlainSHA256: []byte{3}})

	src := &fakeSource{expiredPaths: map[string]bool{"/v/old.enc?oh=1": true}}
	f := &Fetcher{DB: db, Src: src, Dir: dir}
	res, err := f.Fetch(ctx, conv, "IMG1")
	if err != nil {
		t.Fatal(err)
	}
	if src.reuploaded != 1 || len(src.downloads) != 2 {
		t.Fatalf("reupload=%d downloads=%v", src.reuploaded, src.downloads)
	}
	want := filepath.Join(dir, "120363000000000001_g.us", "image_IMG1.jpg")
	if res.Path != want || res.Kind != "image" {
		t.Fatalf("result %+v, want %s", res, want)
	}
	data, _ := os.ReadFile(res.Path)
	if string(data) != "data from /v/fresh/IMG1.enc?oh=new" {
		t.Fatalf("content %q (the partial first attempt must be discarded)", data)
	}
	m, _ := db.GetMessage(ctx, conv, "IMG1")
	if m.DirectPath != "/v/fresh/IMG1.enc?oh=new" || m.LocalPath != want {
		t.Fatalf("db not updated: %+v", m)
	}
	noPartFiles(t, dir)

	// A second call uses the saved file.
	if _, err := f.Fetch(ctx, conv, "IMG1"); err != nil || len(src.downloads) != 2 {
		t.Fatalf("cached fetch: %v, downloads=%d", err, len(src.downloads))
	}
}

func TestFetchSizeLimit(t *testing.T) {
	db, dir := setup(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "BIG", SentAt: 1, Kind: "video", ByteSize: 3000,
		DirectPath: "/v/big", MediaKey: []byte{1}})
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "LIE", SentAt: 2, Kind: "video", ByteSize: 10,
		DirectPath: "/v/lie", MediaKey: []byte{1}})
	src := &fakeSource{size: 5000}
	f := &Fetcher{DB: db, Src: src, Dir: dir, MaxBytes: 2048}
	if _, err := f.Fetch(ctx, conv, "BIG"); !errors.Is(err, ErrTooLarge) || len(src.downloads) != 0 {
		t.Fatalf("declared size over the limit: %v, downloads %d", err, len(src.downloads))
	}
	if _, err := f.Fetch(ctx, conv, "LIE"); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("real size over the limit: %v", err)
	}
	// The download stops at the limit instead of writing the whole file first.
	f.MaxBytes = 2048
	src.size, src.written = 1<<20, 0
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "HUGE", SentAt: 3, Kind: "video", DirectPath: "/v/huge", MediaKey: []byte{1}})
	if _, err := f.Fetch(ctx, conv, "HUGE"); !errors.Is(err, ErrTooLarge) || src.written > 2048+downloadSlack {
		t.Fatalf("stream not stopped: %v after %d bytes", err, src.written)
	}
	noPartFiles(t, dir)
	if entries, _ := os.ReadDir(filepath.Join(dir, "15550100002_s.whatsapp.net")); len(entries) != 0 {
		t.Fatalf("files left: %v", entries)
	}
}

func TestFetchReuploadFails(t *testing.T) {
	db, dir := setup(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "V1", SentAt: 10, Kind: "voice", Mime: "audio/ogg; codecs=opus",
		DirectPath: "/v/gone", MediaKey: []byte{1}})
	src := &fakeSource{expiredPaths: map[string]bool{"/v/gone": true}, reuploadErr: errors.New("phone offline")}
	_, err := (&Fetcher{DB: db, Src: src, Dir: dir}).Fetch(ctx, conv, "V1")
	if err == nil || !strings.Contains(err.Error(), "phone offline") || !Expired(err) {
		t.Fatalf("want re-upload error, got %v", err)
	}
}

func TestFetchErrors(t *testing.T) {
	db, dir := setup(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "T1", SentAt: 1, Body: "just text"})
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "D1", SentAt: 2, Kind: "document"})
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "L1", SentAt: 3, Kind: "location", Body: "here"})
	f := &Fetcher{DB: db, Src: &fakeSource{}, Dir: dir}
	if _, err := f.Fetch(ctx, conv, "T1"); !errors.Is(err, ErrNotMedia) {
		t.Fatalf("text: %v", err)
	}
	if _, err := f.Fetch(ctx, conv, "L1"); !errors.Is(err, ErrNotMedia) {
		t.Fatalf("location: %v", err)
	}
	if _, err := f.Fetch(ctx, conv, "D1"); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("incomplete: %v", err)
	}
	if _, err := f.Fetch(ctx, conv, "nope"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("missing: %v", err)
	}
}

func TestFileNames(t *testing.T) {
	cases := []struct {
		m       store.Message
		private bool
		want    string
	}{
		{store.Message{ID: "ABC", Kind: "image", Mime: "image/png"}, false, "image_ABC.png"},
		{store.Message{ID: "ABC", Kind: "voice", Mime: "audio/ogg; codecs=opus"}, false, "voice_ABC.ogg"},
		{store.Message{ID: "ABC", Kind: "video"}, false, "video_ABC.mp4"},
		{store.Message{ID: "3EB0123456789", Kind: "document", AttachmentName: `..\..\report.pdf`}, false, "report_3EB0123456.pdf"},
		{store.Message{ID: "ABC", Kind: "document", AttachmentName: "C:report.pdf"}, false, "report_ABC.pdf"},
		{store.Message{ID: "ABC", Kind: "document", AttachmentName: "\\\\server\\share\\NUL.txt"}, false, "_NUL_ABC.txt"},
		{store.Message{ID: "ABC", Kind: "document", AttachmentName: "folder/..", Mime: "application/pdf"}, false, "document_ABC.pdf"},
		{store.Message{ID: "ABC", Kind: "document", AttachmentName: "a/b\\notes\t1.txt"}, false, "notes_1_ABC.txt"},
		{store.Message{ID: "3EB0123456789", Kind: "document", AttachmentName: "salary review.pdf"}, true, "document_3EB0123456789.pdf"},
		{store.Message{ID: "ABC", Kind: "document", Mime: "application/pdf"}, false, "document_ABC.pdf"},
		{store.Message{ID: "ABC", Kind: "sticker", AttachmentName: "sticker_0011223344556677.webp"}, true, "sticker_0011223344556677.webp"},
		{store.Message{ID: "ABC", Kind: "sticker", AttachmentName: "folder\\sticker_01.webp"}, false, "sticker_01.webp"},
		{store.Message{ID: "a/b:c", Kind: "image"}, false, "image_a_b_c.jpg"},
	}
	for _, c := range cases {
		if got := FileName(&c.m, c.private); got != c.want {
			t.Errorf("%+v private=%v: got %q want %q", c.m, c.private, got, c.want)
		}
	}
}

func TestSafeName(t *testing.T) {
	cases := map[string]string{
		"Résumé 2026.pdf":            "Résumé 2026.pdf",
		"ΑΘΗΝΑ notes.txt":            "ΑΘΗΝΑ notes.txt",
		"evil‮txt.exe":               "evil_txt.exe",
		"tab\tand\nnewline":          "tab_and_newline",
		"15550100002@s.whatsapp.net": "15550100002_s.whatsapp.net",
		"a/b\\c:d*e?f\"g<h>i|j":      "a_b_c_d_e_f_g_h_i_j",
		"  .hidden. ":                "hidden",
		"CON .txt":                   "_CON .txt",
		"CON.txt":                    "_CON.txt",
		"nul":                        "_nul",
		"":                           "_",
		"‏⁦x⁩":                       "__x_",
		"emoji \U0001F600 face":      "emoji _ face",
	}
	for in, want := range cases {
		if got := SafeName(in); got != want {
			t.Errorf("SafeName(%q) = %q, want %q", in, got, want)
		}
	}
	long := strings.Repeat("é", 200) + ".pdf"
	got := SafeName(long)
	if utf8.RuneCountInString(got) != 120 || !utf8.ValidString(got) {
		t.Fatalf("long name: %d runes, valid=%v", utf8.RuneCountInString(got), utf8.ValidString(got))
	}
	if got := SafeName(strings.Repeat("a", 119) + " .x"); strings.HasSuffix(got, " ") || strings.HasSuffix(got, ".") {
		t.Fatalf("cut name must be trimmed again: %q", got)
	}
}

type fakePictures struct{ url string }

func (f fakePictures) ProfilePicture(ctx context.Context, jid types.JID) (string, error) {
	return f.url, nil
}

func TestSaveAvatar(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("jpeg bytes"))
	}))
	defer srv.Close()
	dir := t.TempDir()
	jid := types.NewJID("15550100002", types.DefaultUserServer)
	path, err := SaveAvatar(context.Background(), fakePictures{srv.URL}, srv.Client(), dir, jid)
	if err != nil {
		t.Fatal(err)
	}
	if path != filepath.Join(dir, "avatars", "15550100002.jpg") {
		t.Fatalf("path %s", path)
	}
	big := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "999999999")
		w.WriteHeader(http.StatusOK)
	}))
	defer big.Close()
	if _, err := SaveAvatar(context.Background(), fakePictures{big.URL}, big.Client(), dir, jid); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("declared size over the limit: %v", err)
	}
	if _, err := SaveAvatar(context.Background(), fakePictures{""}, nil, dir, jid); !errors.Is(err, ErrNoAvatar) {
		t.Fatalf("no picture: %v", err)
	}
}
