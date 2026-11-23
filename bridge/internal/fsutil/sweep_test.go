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
	other := filepath.Join(root, "work")
	os.MkdirAll(other, 0o700)
	os.WriteFile(filepath.Join(other, ".part-9"), []byte("x"), 0o600)
	if n := SweepTemp(root, []string{other}, ".part-*"); n != 1 {
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
	if _, err := os.Stat(filepath.Join(other, ".part-9")); err != nil {
		t.Fatal("a file of another account was removed")
	}
	if SweepTemp(filepath.Join(root, "missing"), nil, "*") != 0 {
		t.Fatal("missing root")
	}
}

func TestSweepTempInNestedAccount(t *testing.T) {
	parent := t.TempDir()
	child := filepath.Join(parent, "personal")
	if err := os.Mkdir(child, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{parent, child} {
		if err := os.WriteFile(filepath.Join(dir, ".part-test"), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if n := SweepTemp(child, []string{parent}, ".part-*"); n != 1 {
		t.Fatalf("removed %d", n)
	}
	if _, err := os.Stat(filepath.Join(parent, ".part-test")); err != nil {
		t.Fatal(err)
	}
}

func TestSweepPreservesHardLinks(t *testing.T) {
	root := t.TempDir()
	original := filepath.Join(t.TempDir(), "original")
	if err := os.WriteFile(original, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(original, filepath.Join(root, ".part-copy")); err != nil {
		t.Fatal(err)
	}
	if SweepTemp(root, nil, ".part-*") != 0 {
		t.Fatal("reported a hard-linked file as wiped")
	}
	if data, err := os.ReadFile(original); err != nil || string(data) != "keep" {
		t.Fatalf("original changed: %q, %v", data, err)
	}
}
