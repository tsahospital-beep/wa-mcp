package fsutil

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestWriteFileAndLock(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "store")
	path := filepath.Join(dir, "api.token")
	if err := WriteFile(path, []byte("abc")); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(path)
	if err != nil || string(b) != "abc" {
		t.Fatalf("read back %q %v", b, err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Fatalf("temporary files left: %v", entries)
	}

	release, err := Lock(filepath.Join(dir, "bridge.lock"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Lock(filepath.Join(dir, "bridge.lock")); !errors.Is(err, ErrLocked) {
		t.Fatalf("second lock: %v", err)
	}
	release()
	again, err := Lock(filepath.Join(dir, "bridge.lock"))
	if err != nil {
		t.Fatalf("lock after release: %v", err)
	}
	again()
}

func TestInside(t *testing.T) {
	root := filepath.Join(t.TempDir(), "Store")
	if !Inside(root, filepath.Join(root, "wa.db")) || !Inside(root, root) {
		t.Fatal("inside")
	}
	if Inside(root, filepath.Join(root+"2", "x")) || Inside(root, filepath.Dir(root)) {
		t.Fatal("outside")
	}
	if runtime.GOOS == "windows" && !Inside(root, strings.ToUpper(filepath.Join(root, "wa.db"))) {
		t.Fatal("case must be ignored on Windows")
	}
}

func TestRestrictedPermissions(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "store")
	os.MkdirAll(dir, 0o755)
	file := filepath.Join(dir, "wa.db")
	os.WriteFile(file, []byte("x"), 0o644)
	if err := RestrictTree(dir); err != nil {
		t.Fatal(err)
	}
	checkPrivate(t, dir, true)
	checkPrivate(t, file, false)
}

func TestPublishStaysInsideRoot(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	tmp := filepath.Join(base, "partial")
	if err := os.WriteFile(tmp, []byte("report"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Publish(base, tmp, filepath.Join(outside, "report")); err == nil {
		t.Fatal("outside destination accepted")
	}
	linkTestDir(t, outside, filepath.Join(base, "chat"))
	if err := Publish(base, tmp, filepath.Join(base, "chat", "report")); err == nil {
		t.Fatal("linked destination accepted")
	}
	if _, err := os.Stat(filepath.Join(outside, "report")); !os.IsNotExist(err) {
		t.Fatal("unexpected output")
	}
}
