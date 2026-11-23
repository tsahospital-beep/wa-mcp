package sender

import (
	"fmt"
	"os"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// PathError explains why a media path was refused. Only the base name is
// shown, so logs and stored errors do not reveal folder names.
type PathError struct {
	Path   string
	Reason string
}

func (e *PathError) Error() string {
	return fmt.Sprintf("media file %q: %s", common.BaseName(e.Path), e.Reason)
}

// Reasons used by OpenMedia.
const (
	reasonOutside = "outside the allowed folders (downloads_dir, exports_dir, sending.allowed_media_dirs)"
	reasonDenied  = "a file of the bridge store or the config file; these are never sent"
	reasonLinked  = fsutil.HardLinked
	reasonJunc    = fsutil.LinkedPath
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
func OpenMedia(path string, allowed, denied []string, checks ...func(string) bool) (*os.File, error) {
	f, reason := fsutil.OpenRegular(path, allowed, denied, checks...)
	switch reason {
	case "":
		return f, nil
	case fsutil.OutsideFolders:
		reason = reasonOutside
	case fsutil.DeniedPath:
		reason = reasonDenied
	}
	return nil, &PathError{Path: path, Reason: reason}
}

// CheckPath runs the OpenMedia checks and returns the resolved path.
func CheckPath(path string, allowed, denied []string, checks ...func(string) bool) (string, error) {
	f, err := OpenMedia(path, allowed, denied, checks...)
	if err != nil {
		return "", err
	}
	defer f.Close()
	return f.Name(), nil
}
