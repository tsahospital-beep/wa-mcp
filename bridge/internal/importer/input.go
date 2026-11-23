package importer

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

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
func OpenInput(path string, allowed, denied []string) (*os.File, error) {
	if strings.TrimSpace(path) == "" {
		return nil, &InputError{Path: path, Reason: "path is empty"}
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, &InputError{Path: path, Reason: "bad path"}
	}
	if err := fsutil.CheckNoLinks(abs); err != nil {
		return nil, &InputError{Path: path, Reason: "goes through a link or junction; use the real folder", Forbidden: true}
	}
	real := fsutil.Resolve(abs)
	f, err := os.Open(real)
	if err != nil {
		return nil, &InputError{Path: path, Reason: "file not found", Missing: true}
	}
	refuse := func(reason string, forbidden bool) (*os.File, error) {
		f.Close()
		return nil, &InputError{Path: path, Reason: reason, Forbidden: forbidden}
	}
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return refuse("not a regular file", false)
	}
	if n, err := fsutil.LinkCount(f); err != nil || n > 1 {
		return refuse("has more than one hard link; copy the file instead", true)
	}
	for _, d := range denied {
		if d != "" && fsutil.Inside(fsutil.Resolve(d), real) {
			return refuse("a file of the bridge store or the config file; these are never imported", true)
		}
	}
	if fsutil.SameAsAny(st, denied) {
		return refuse("a file of the bridge store or the config file; these are never imported", true)
	}
	if len(allowed) == 0 {
		return f, nil
	}
	for _, dir := range allowed {
		if fsutil.Inside(fsutil.Resolve(dir), real) {
			return f, nil
		}
	}
	return refuse("outside the allowed folders (downloads_dir, exports_dir, import.allowed_dirs)", true)
}
