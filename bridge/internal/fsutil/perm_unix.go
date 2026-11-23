//go:build !windows

package fsutil

import (
	"errors"
	"os"
	"syscall"

	"golang.org/x/sys/unix"
)

func restrictDir(path string) error  { return os.Chmod(path, 0o700) }
func restrictFile(path string) error { return os.Chmod(path, 0o600) }

// Lock takes an exclusive lock on path (created if missing). The returned
// function releases it.
func Lock(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		f.Close()
		return nil, ErrLocked
	}
	return func() {
		unix.Flock(int(f.Fd()), unix.LOCK_UN)
		f.Close()
	}, nil
}

// LinkCount returns the number of hard links of an open file.
func LinkCount(f *os.File) (uint32, error) {
	st, err := f.Stat()
	if err != nil {
		return 0, err
	}
	sys, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, errors.New("no link count available")
	}
	return uint32(sys.Nlink), nil
}

// CheckNoLinks is a no-op on Unix: symbolic links are resolved before the
// allow and deny checks, and hard links are refused by LinkCount.
func CheckNoLinks(abs string) error { return nil }
