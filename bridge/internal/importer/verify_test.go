package importer

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestSameEntryChecksTheCRC(t *testing.T) {
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, _ := zw.Create("IMG-20260101-WA0001.jpg")
	w.Write([]byte("photo bytes"))
	zw.Close()
	zr, err := zip.NewReader(bytes.NewReader(buf.Bytes()), int64(buf.Len()))
	if err != nil {
		t.Fatal(err)
	}
	entry := zr.File[0]
	target := filepath.Join(t.TempDir(), "image_imp-1.jpg")
	os.WriteFile(target, []byte("photo bytez"), 0o600) // same size, other bytes
	if sameEntry(target, entry) {
		t.Fatal("a replaced file was reused")
	}
	os.WriteFile(target, []byte("photo bytes"), 0o600)
	if !sameEntry(target, entry) {
		t.Fatal("the intact file was not reused")
	}
}
