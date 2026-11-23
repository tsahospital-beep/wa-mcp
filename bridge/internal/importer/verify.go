package importer

import (
	"archive/zip"
	"hash/crc32"
	"io"
	"os"
)

// sameEntry reports whether the file at target already holds the zip entry:
// same size and the same CRC-32 as the entry header, so a file that was
// replaced or cut short is written again.
func sameEntry(target string, f *zip.File) bool {
	st, err := os.Stat(target)
	if err != nil || !st.Mode().IsRegular() || st.Size() != int64(f.UncompressedSize64) {
		return false
	}
	in, err := os.Open(target)
	if err != nil {
		return false
	}
	defer in.Close()
	h := crc32.NewIEEE()
	if _, err := io.Copy(h, in); err != nil {
		return false
	}
	return h.Sum32() == f.CRC32
}
