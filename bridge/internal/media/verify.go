package media

import (
	"bytes"
	"crypto/sha256"
	"io"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// sameFile reports whether the file at path is the attachment: its size
// matches when the size is known, and its SHA-256 when the hash is known, so
// a file that was replaced or cut short is downloaded again.
func sameFile(path string, size int64, plainSHA256 []byte, roots ...string) bool {
	f, _ := fsutil.OpenRegular(path, roots, nil)
	if f == nil {
		return false
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || (size > 0 && st.Size() != size) {
		return false
	}
	if len(plainSHA256) != sha256.Size {
		return true
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return false
	}
	return bytes.Equal(h.Sum(nil), plainSHA256)
}
