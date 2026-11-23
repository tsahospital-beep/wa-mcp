package importer

import (
	"archive/zip"
	"hash/crc32"
	"io"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// sameEntry reports whether the file at target already holds the zip entry:
// same size and the same CRC-32 as the entry header, so a file that was
// replaced or cut short is written again.
func sameEntry(target string, f *zip.File, roots ...string) bool {
	in, _ := fsutil.OpenRegular(target, roots, nil)
	if in == nil {
		return false
	}
	defer in.Close()
	st, err := in.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Size() != int64(f.UncompressedSize64) {
		return false
	}
	h := crc32.NewIEEE()
	if _, err := io.Copy(h, in); err != nil {
		return false
	}
	return h.Sum32() == f.CRC32
}
