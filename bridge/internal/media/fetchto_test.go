package media

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestFetchToWritesCallerPathAndKeepsLocalPath(t *testing.T) {
	db, dir := setup(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	if err := db.PutMessage(ctx, &store.Message{Conv: conv, ID: "IMG1", SentAt: 1, Kind: store.KindImage,
		DirectPath: "/v/img1.enc", MediaKey: []byte("k")}); err != nil {
		t.Fatal(err)
	}
	src := &fakeSource{}
	f := &Fetcher{DB: db, Src: src, Dir: dir}
	target := filepath.Join(t.TempDir(), "elsewhere", "image_IMG1.jpg")
	res, err := f.FetchTo(ctx, conv, "IMG1", target)
	if err != nil || res.Path != target || res.Kind != store.KindImage {
		t.Fatalf("FetchTo: %+v %v", res, err)
	}
	if b, _ := os.ReadFile(target); string(b) != "data from /v/img1.enc" {
		t.Fatalf("content %q", b)
	}
	m, _ := db.GetMessage(ctx, conv, "IMG1")
	if m.LocalPath != "" {
		t.Fatalf("FetchTo changed local_path to %q", m.LocalPath)
	}
	// A file already there is reused without a download.
	if _, err := f.FetchTo(ctx, conv, "IMG1", target); err != nil || len(src.downloads) != 1 {
		t.Fatalf("second FetchTo: %v, downloads %d", err, len(src.downloads))
	}
	if _, err := f.FetchTo(ctx, conv, "IMG1", ""); err == nil {
		t.Fatal("empty target accepted")
	}
	noPartFiles(t, filepath.Dir(target))
}
