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
	if st.Mode().IsRegular() {
		if f, err := os.OpenFile(path, os.O_WRONLY, 0); err == nil {
			zero := make([]byte, 64<<10)
			for left := st.Size(); left > 0; {
				n := int64(len(zero))
				if left < n {
					n = left
				}
				if _, err := f.Write(zero[:n]); err != nil {
					break
				}
				left -= n
			}
			f.Sync()
			f.Close()
		}
	}
	return os.Remove(path)
}
