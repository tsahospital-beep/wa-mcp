package sender

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// PathError explains why a media path was refused. Only the base name is
// shown, so logs and stored errors do not reveal folder names.
type PathError struct {
	Path   string
	Reason string
}

func (e *PathError) Error() string {
	return fmt.Sprintf("media file %q: %s", filepath.Base(e.Path), e.Reason)
}

// Reasons used by OpenMedia.
const (
	reasonOutside = "outside the allowed folders (downloads_dir, exports_dir, sending.allowed_media_dirs)"
	reasonDenied  = "a file of the bridge store or the config file; these are never sent"
	reasonLinked  = "has more than one hard link; copy the file instead"
	reasonJunc    = "goes through a link or junction; use the real folder"
)

// Forbidden reports whether the path was refused by a rule (as opposed to a
// missing or unreadable file).
func (e *PathError) Forbidden() bool {
	switch e.Reason {
	case reasonOutside, reasonDenied, reasonLinked, reasonJunc:
		return true
	}
	return false
}

// OpenMedia opens a file for sending and checks the open handle, so the file
// that is checked is the one that gets uploaded: a regular file with a single
// link, no junction or symbolic link on its path (Windows), not one of the
// denied files under any name, and inside an allowed folder (any folder when
// allowed is empty).
func OpenMedia(path string, allowed, denied []string) (*os.File, error) {
	if strings.TrimSpace(path) == "" {
		return nil, &PathError{Path: path, Reason: "empty"}
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, &PathError{Path: path, Reason: "bad path"}
	}
	if err := fsutil.CheckNoLinks(abs); err != nil {
		return nil, &PathError{Path: path, Reason: reasonJunc}
	}
	real := fsutil.Resolve(abs)
	f, err := os.Open(real)
	if err != nil {
		return nil, &PathError{Path: path, Reason: "file not found"}
	}
	fail := func(reason string) (*os.File, error) {
		f.Close()
		return nil, &PathError{Path: path, Reason: reason}
	}
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return fail("not a regular file")
	}
	if n, err := fsutil.LinkCount(f); err != nil || n > 1 {
		return fail(reasonLinked)
	}
	for _, d := range denied {
		if d != "" && fsutil.Inside(fsutil.Resolve(d), real) {
			return fail(reasonDenied)
		}
	}
	if fsutil.SameAsAny(st, denied) {
		return fail(reasonDenied)
	}
	if len(allowed) == 0 {
		return f, nil
	}
	for _, dir := range allowed {
		if fsutil.Inside(fsutil.Resolve(dir), real) {
			return f, nil
		}
	}
	return fail(reasonOutside)
}

// CheckPath runs the OpenMedia checks and returns the resolved path.
func CheckPath(path string, allowed, denied []string) (string, error) {
	f, err := OpenMedia(path, allowed, denied)
	if err != nil {
		return "", err
	}
	defer f.Close()
	return f.Name(), nil
}
