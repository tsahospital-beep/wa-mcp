package fsutil

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSweepTempRemovesOnlyLeftovers(t *testing.T) {
	root := t.TempDir()
	chat := filepath.Join(root, "15550100002_s.whatsapp.net")
	os.MkdirAll(chat, 0o700)
	for _, name := range []string{".part-123", "image_A1.jpg", "upload-9"} {
		os.WriteFile(filepath.Join(chat, name), []byte("x"), 0o600)
	}
	if n := SweepTemp(root, ".part-*"); n != 1 {
		t.Fatalf("removed %d", n)
	}
	if _, err := os.Stat(filepath.Join(chat, ".part-123")); !os.IsNotExist(err) {
		t.Fatal("leftover kept")
	}
	for _, name := range []string{"image_A1.jpg", "upload-9"} {
		if _, err := os.Stat(filepath.Join(chat, name)); err != nil {
			t.Fatalf("%s removed", name)
		}
	}
	if SweepTemp(filepath.Join(root, "missing"), "*") != 0 {
		t.Fatal("missing root")
	}
}
