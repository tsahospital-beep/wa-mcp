package media

import (
	"context"
	"crypto/sha256"
	"os"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestReplacedFileIsDownloadedAgain(t *testing.T) {
	db, dir := setup(t)
	ctx := context.Background()
	conv := "15550100002@s.whatsapp.net"
	content := []byte("data from /v/doc.enc")
	sum := sha256.Sum256(content)
	db.PutMessage(ctx, &store.Message{Conv: conv, ID: "DOC1", SentAt: 10, Kind: "document", Mime: "application/pdf",
		AttachmentName: "plan.pdf", DirectPath: "/v/doc.enc", MediaKey: []byte{1}, EncSHA256: []byte{2},
		PlainSHA256: sum[:], ByteSize: int64(len(content))})
	src := &fakeSource{}
	f := &Fetcher{DB: db, Src: src, Dir: dir}
	res, err := f.Fetch(ctx, conv, "DOC1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Fetch(ctx, conv, "DOC1"); err != nil || len(src.downloads) != 1 {
		t.Fatalf("an intact file was downloaded again: %v %d", err, len(src.downloads))
	}
	// Same size, other bytes: the file was replaced.
	os.WriteFile(res.Path, []byte("data from /v/alt.enc"), 0o600)
	if _, err := f.Fetch(ctx, conv, "DOC1"); err != nil || len(src.downloads) != 2 {
		t.Fatalf("a replaced file was used: %v %d", err, len(src.downloads))
	}
	if got, _ := os.ReadFile(res.Path); string(got) != string(content) {
		t.Fatalf("content %q", got)
	}
	noPartFiles(t, dir)
}
