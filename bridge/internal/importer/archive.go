package importer

import (
	"archive/zip"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

var (
	// ErrTooLarge means a file, an archive or its content is above a limit.
	ErrTooLarge = errors.New("too large")
	// ErrUnsafeArchive means the archive has an entry or a size pattern that is never needed for a chat export.
	ErrUnsafeArchive = errors.New("unsafe archive")
)

// Source is the file to import.
type Source struct {
	R    io.ReaderAt
	Size int64
	Name string // original file name, used as a hint for the chat title
}

// Limits bound what an import reads and writes.
type Limits struct {
	MaxBytes      int64 // the file, and the unpacked size of an archive
	MaxEntries    int   // files in an archive
	MaxRatio      int   // unpacked size divided by archive size above which an archive is refused
	MaxMediaBytes int64 // one attachment
}

// Defaults of Limits.
const (
	DefaultMaxBytes      = 1 << 30
	DefaultMaxEntries    = 20000
	DefaultMaxRatio      = 100
	DefaultMaxMediaBytes = 512 << 20

	maxTextBytes = 64 << 20 // the chat text, whatever MaxBytes is
	ratioFloor   = 8 << 20  // smaller archives are never judged by their ratio
)

func (l Limits) withDefaults() Limits {
	if l.MaxBytes <= 0 {
		l.MaxBytes = DefaultMaxBytes
	}
	if l.MaxEntries <= 0 {
		l.MaxEntries = DefaultMaxEntries
	}
	if l.MaxRatio <= 0 {
		l.MaxRatio = DefaultMaxRatio
	}
	if l.MaxMediaBytes <= 0 {
		l.MaxMediaBytes = DefaultMaxMediaBytes
	}
	return l
}

// archive is an opened zip export.
type archive struct {
	zr    *zip.Reader
	files map[string]*zip.File // lower case base name -> entry
	chat  *zip.File
}

func isZip(src Source) bool {
	var magic [4]byte
	if _, err := src.R.ReadAt(magic[:], 0); err != nil {
		return false
	}
	return bytes.Equal(magic[:], []byte("PK\x03\x04")) || bytes.Equal(magic[:], []byte("PK\x05\x06"))
}

// unsafeName reports entry names that escape a folder or address a drive or stream.
func unsafeName(name string) bool {
	if name == "" || strings.ContainsAny(name, "\x00:") || strings.HasPrefix(name, "/") {
		return true
	}
	for _, part := range strings.Split(name, "/") {
		if part == ".." {
			return true
		}
	}
	return false
}

// openArchive checks a zip and finds the chat text. Nothing is unpacked yet.
func openArchive(src Source, lim Limits) (*archive, error) {
	zr, err := zip.NewReader(src.R, src.Size)
	if err != nil && !errors.Is(err, zip.ErrInsecurePath) {
		return nil, fmt.Errorf("not a readable zip file: %w", err)
	}
	if len(zr.File) > lim.MaxEntries {
		return nil, fmt.Errorf("%w: the archive has %d entries (limit %d)", ErrTooLarge, len(zr.File), lim.MaxEntries)
	}
	ar := &archive{zr: zr, files: map[string]*zip.File{}}
	var total uint64
	var txt []*zip.File
	for _, f := range zr.File {
		name := strings.ReplaceAll(f.Name, "\\", "/")
		if unsafeName(name) {
			return nil, fmt.Errorf("%w: entry %q has a path that leaves the folder or names a drive", ErrUnsafeArchive, printable(f.Name))
		}
		if f.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("%w: entry %q is a symbolic link", ErrUnsafeArchive, printable(f.Name))
		}
		if strings.HasSuffix(name, "/") || f.FileInfo().IsDir() {
			continue
		}
		total += f.UncompressedSize64
		base := strings.ToLower(path.Base(name))
		if _, dup := ar.files[base]; !dup {
			ar.files[base] = f
		}
		if strings.HasSuffix(base, ".txt") {
			txt = append(txt, f)
		}
	}
	if total > uint64(lim.MaxBytes) {
		return nil, fmt.Errorf("%w: the archive unpacks to %d MiB (limit %d MiB, import.max_mb)", ErrTooLarge, total>>20, lim.MaxBytes>>20)
	}
	if total > ratioFloor && total/uint64(max(src.Size, 1)) > uint64(lim.MaxRatio) {
		return nil, fmt.Errorf("%w: the archive unpacks to %d times its size, which looks like a zip bomb", ErrUnsafeArchive, total/uint64(max(src.Size, 1)))
	}
	ar.chat = pickChatFile(txt)
	if ar.chat == nil {
		return nil, errors.New("the archive has no .txt file with the chat")
	}
	return ar, nil
}

// printable shortens an entry name for an error message.
func printable(s string) string {
	s = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return '?'
		}
		return r
	}, s)
	if utf8.RuneCountInString(s) > 80 {
		s = string([]rune(s)[:80]) + "..."
	}
	return s
}

// pickChatFile prefers _chat.txt (iOS), then "WhatsApp Chat ...", then the largest .txt.
func pickChatFile(txt []*zip.File) *zip.File {
	if len(txt) == 0 {
		return nil
	}
	rank := func(f *zip.File) int {
		base := strings.ToLower(path.Base(strings.ReplaceAll(f.Name, "\\", "/")))
		switch {
		case base == "_chat.txt":
			return 0
		case strings.HasPrefix(base, "whatsapp chat") || strings.HasPrefix(base, "whatsapp-chat"):
			return 1
		}
		return 2
	}
	sort.SliceStable(txt, func(i, j int) bool {
		ri, rj := rank(txt[i]), rank(txt[j])
		if ri != rj {
			return ri < rj
		}
		return txt[i].UncompressedSize64 > txt[j].UncompressedSize64
	})
	return txt[0]
}

// readEntry reads an entry completely, refusing more than max bytes however large its header says it is.
func readEntry(f *zip.File, max int64, lim Limits) ([]byte, error) {
	if f.Flags&0x1 != 0 {
		return nil, errors.New("the chat file in the archive is password protected")
	}
	if err := ratioOK(f, lim); err != nil {
		return nil, err
	}
	rc, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer rc.Close()
	data, err := io.ReadAll(io.LimitReader(rc, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > max {
		return nil, fmt.Errorf("%w: the chat file is larger than %d MiB", ErrTooLarge, max>>20)
	}
	return data, nil
}

// ratioOK refuses one entry that unpacks to far more than its stored size.
func ratioOK(f *zip.File, lim Limits) error {
	if f.UncompressedSize64 > ratioFloor/8 && f.UncompressedSize64/max(f.CompressedSize64, 1) > uint64(lim.MaxRatio) {
		return fmt.Errorf("%w: entry %q unpacks to %d times its size", ErrUnsafeArchive, printable(f.Name), f.UncompressedSize64/max(f.CompressedSize64, 1))
	}
	return nil
}

// readPlain reads a text file with the same size rule as an archive entry.
func readPlain(src Source, max int64) ([]byte, error) {
	if src.Size > max {
		return nil, fmt.Errorf("%w: the file is larger than %d MiB", ErrTooLarge, max>>20)
	}
	data, err := io.ReadAll(io.NewSectionReader(src.R, 0, src.Size))
	if err != nil {
		return nil, err
	}
	return data, nil
}

// decodeText turns file bytes into a string: UTF-8 (with or without BOM) or UTF-16 with a BOM.
func decodeText(b []byte) string {
	switch {
	case bytes.HasPrefix(b, []byte{0xff, 0xfe}):
		return utf16Text(b[2:], false)
	case bytes.HasPrefix(b, []byte{0xfe, 0xff}):
		return utf16Text(b[2:], true)
	}
	b = bytes.TrimPrefix(b, []byte{0xef, 0xbb, 0xbf})
	if utf8.Valid(b) {
		return string(b)
	}
	return strings.ToValidUTF8(string(b), "\ufffd")
}

func utf16Text(b []byte, bigEndian bool) string {
	units := make([]uint16, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		if bigEndian {
			units = append(units, uint16(b[i])<<8|uint16(b[i+1]))
		} else {
			units = append(units, uint16(b[i+1])<<8|uint16(b[i]))
		}
	}
	return string(utf16.Decode(units))
}
