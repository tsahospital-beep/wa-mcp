package fsutil

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
)

func TestOpenRegularChecksFileAndRoots(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "note.txt")
	if err := os.WriteFile(path, []byte("note"), 0o600); err != nil {
		t.Fatal(err)
	}
	f, reason := OpenRegular(path, []string{dir}, nil)
	if f == nil || reason != "" {
		t.Fatalf("open: %s", reason)
	}
	defer f.Close()
	b, err := io.ReadAll(f)
	if err != nil || string(b) != "note" {
		t.Fatalf("read: %q %v", b, err)
	}
	for _, tc := range []struct {
		path            string
		allowed, denied []string
		want            string
	}{
		{"", nil, nil, "empty"},
		{dir, nil, nil, "not a regular file"},
		{path, []string{t.TempDir()}, nil, OutsideFolders},
		{path, []string{dir}, []string{dir}, DeniedPath},
		{path, nil, []string{path}, DeniedPath},
	} {
		if f, reason := OpenRegular(tc.path, tc.allowed, tc.denied); f != nil || reason != tc.want {
			t.Errorf("open %q: got %q, want %q", tc.path, reason, tc.want)
		}
	}
	if f, reason := OpenRegular(path, nil, nil, func(real string) bool { return real == Resolve(path) }); f != nil || reason != DeniedPath {
		t.Fatalf("account check: %s", reason)
	}
}

func linkTestDir(t *testing.T, target, link string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
			"$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:WA_TEST_LINK -Target $env:WA_TEST_TARGET | Out-Null")
		cmd.Env = append(os.Environ(), "WA_TEST_LINK="+link, "WA_TEST_TARGET="+target)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("junction: %v: %s", err, out)
		}
	} else if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
}

func TestOpenedPathMustBeResolved(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(target, []byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "alias")
	linkTestDir(t, filepath.Dir(target), link)
	if f, reason := openResolved(filepath.Join(link, filepath.Base(target))); f != nil || reason == "" {
		t.Fatalf("changed path: %v", reason)
	}
}
