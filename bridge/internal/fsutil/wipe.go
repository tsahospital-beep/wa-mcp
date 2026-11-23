package fsutil

import (
	"errors"
	"os"
)

// Wipe overwrites a regular file with zeros and removes it. On SSDs and
// copy-on-write file systems old blocks may survive; full disk encryption
// covers that case. A missing file is not an error.
func Wipe(path string) error {
	st, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !st.Mode().IsRegular() {
		if st.Mode()&os.ModeSymlink != 0 {
			return os.Remove(path)
		}
		return errors.New("refusing to wipe a non-regular file")
	}
	f, err := os.OpenFile(path, os.O_WRONLY|nonblockFlag, 0)
	if err != nil {
		return err
	}
	err = wipeRegular(f, st)
	if err := errors.Join(err, f.Close()); err != nil {
		return err
	}
	return os.Remove(path)
}

func wipeRegular(f *os.File, expected os.FileInfo) error {
	st, err := f.Stat()
	if err != nil {
		return err
	}
	if !os.SameFile(st, expected) || !st.Mode().IsRegular() {
		return errors.New("file changed before wipe")
	}
	n, err := LinkCount(f)
	if err != nil {
		return err
	}
	if n != 1 {
		return errors.New("refusing to wipe a file with multiple hard links")
	}
	zero := make([]byte, 64<<10)
	for left := st.Size(); left > 0; {
		n, err := f.Write(zero[:min(left, int64(len(zero)))])
		if err != nil {
			return err
		}
		left -= int64(n)
	}
	return f.Sync()
}
