package importer

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// InputError explains why an import file was refused. Only the base name is shown, so messages do
// not reveal folder names.
type InputError struct {
	Path      string
	Reason    string
	Forbidden bool // refused by a rule (as opposed to a missing or unusable file)
	Missing   bool // the file does not exist
}

func (e *InputError) Error() string {
	return fmt.Sprintf("import file %q: %s", filepath.Base(e.Path), e.Reason)
}

// OpenInput opens a file to import and checks the open handle, so the file that is checked is the
// one that is read: a regular file with a single link, no junction or symbolic link on its path
// (Windows), not one of the denied files or folders, and inside an allowed folder (anywhere when
// allowed is empty).
func OpenInput(path string, allowed, denied []string, checks ...func(string) bool) (*os.File, error) {
	f, reason := fsutil.OpenRegular(path, allowed, denied, checks...)
	if reason == "" {
		return f, nil
	}
	e := &InputError{Path: path, Reason: reason}
	switch reason {
	case "empty":
		e.Reason = "path is empty"
	case "file not found":
		e.Missing = true
	case fsutil.HardLinked, fsutil.LinkedPath:
		e.Forbidden = true
	case fsutil.DeniedPath:
		e.Forbidden = true
		e.Reason = "a file of the bridge store or the config file; these are never imported"
	case fsutil.OutsideFolders:
		e.Forbidden = true
		e.Reason = "outside the allowed folders (downloads_dir, exports_dir, import.allowed_dirs)"
	}
	return nil, e
}
