package fsutil

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// fakeEFS keeps encryption state in memory, so tests never encrypt real files
// (a first real EFS use creates a certificate for the user).
type fakeEFS struct {
	encrypted map[string]bool
	dirStatus uint32
	failOn    string
}

func (f *fakeEFS) ops() efsOps {
	return efsOps{
		status: func(p string) (uint32, error) {
			if f.encrypted[p] {
				return efsIsEncrypted, nil
			}
			if st, err := os.Stat(p); err == nil && st.IsDir() {
				return f.dirStatus, nil
			}
			return efsEncryptable, nil
		},
		encrypt: func(p string) error {
			if filepath.Base(p) == f.failOn {
				return errors.New("The process cannot access the file because it is being used by another process.")
			}
			f.encrypted[p] = true
			return nil
		},
	}
}

func TestApplyEFS(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"session.db", "wa.db", "bridge.lock"} {
		os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o600)
	}
	f := &fakeEFS{encrypted: map[string]bool{}}
	res := applyEFS(dir, map[string]bool{"bridge.lock": true}, f.ops())
	if res.State != EFSOn || !f.encrypted[dir] || !f.encrypted[filepath.Join(dir, "session.db")] || f.encrypted[filepath.Join(dir, "bridge.lock")] {
		t.Fatalf("result %+v, encrypted %v", res, f.encrypted)
	}

	f2 := &fakeEFS{encrypted: map[string]bool{}, failOn: "wa.db"}
	if res := applyEFS(dir, nil, f2.ops()); res.State != EFSPartial || len(res.Failed) != 1 || res.Failed[0] != "wa.db" {
		t.Fatalf("partial %+v", res)
	}

	f3 := &fakeEFS{encrypted: map[string]bool{}, dirStatus: efsNotSupported}
	if res := applyEFS(dir, nil, f3.ops()); res.State != EFSUnavailable || res.Reason == "" || len(f3.encrypted) != 0 {
		t.Fatalf("unsupported %+v", res)
	}
}

func TestEFSStatusCallWorks(t *testing.T) {
	ops, ok := platformEFS()
	if runtime.GOOS != "windows" {
		if ok || EncryptEFS(t.TempDir()).State != EFSNotWindows {
			t.Fatal("EFS outside Windows")
		}
		return
	}
	if !ok {
		t.Skip("advapi32 has no EFS functions")
	}
	// Only the read-only status call runs here.
	if _, err := ops.status(t.TempDir()); err != nil {
		t.Fatalf("status: %v", err)
	}
}
