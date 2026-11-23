package fsutil

import (
	"os"
	"path/filepath"
	"strings"
)

const (
	LinkedPath     = "goes through a link or junction; use the real folder"
	HardLinked     = "has more than one hard link; copy the file instead"
	DeniedPath     = "protected path"
	OutsideFolders = "outside allowed folders"
)

// OpenRegular checks the opened handle and returns a refusal reason on failure.
func OpenRegular(path string, allowed, denied []string, checks ...func(string) bool) (*os.File, string) {
	if strings.TrimSpace(path) == "" {
		return nil, "empty"
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, "bad path"
	}
	if err := CheckNoLinks(abs); err != nil {
		return nil, LinkedPath
	}
	real := Resolve(abs)
	f, reason := openResolved(real)
	if reason != "" {
		return nil, reason
	}
	fail := func(reason string) (*os.File, string) {
		f.Close()
		return nil, reason
	}
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return fail("not a regular file")
	}
	if n, err := LinkCount(f); err != nil || n > 1 {
		return fail(HardLinked)
	}
	for _, d := range denied {
		if d != "" && Inside(Resolve(d), real) {
			return fail(DeniedPath)
		}
	}
	for _, refused := range checks {
		if refused(real) {
			return fail(DeniedPath)
		}
	}
	if SameAsAny(st, denied) {
		return fail(DeniedPath)
	}
	if len(allowed) == 0 {
		return f, ""
	}
	for _, dir := range allowed {
		if containsFile(Resolve(dir), real) {
			return f, ""
		}
	}
	return fail(OutsideFolders)
}

// containsFile checks directory identity on volumes that can be case-sensitive.
func containsFile(dir, path string) bool {
	if !Inside(dir, path) {
		return false
	}
	root, err := os.Stat(dir)
	if err != nil || !root.IsDir() {
		return false
	}
	for parent := filepath.Dir(path); ; parent = filepath.Dir(parent) {
		if st, err := os.Stat(parent); err == nil && os.SameFile(root, st) {
			return true
		}
		if parent == filepath.Dir(parent) {
			return false
		}
	}
}

// Open each resolved component relative to its pinned parent.
func openResolved(path string) (*os.File, string) {
	volume := filepath.VolumeName(path) + string(filepath.Separator)
	root, err := os.OpenRoot(volume)
	if err != nil {
		return nil, "file not found"
	}
	defer func() { root.Close() }()
	parts := strings.Split(strings.TrimPrefix(path, volume), string(filepath.Separator))
	for i, part := range parts {
		if part == "" {
			return nil, "not a regular file"
		}
		st, err := root.Lstat(part)
		if err != nil {
			return nil, "file not found"
		}
		if st.Mode()&os.ModeSymlink != 0 {
			return nil, LinkedPath
		}
		if i == len(parts)-1 {
			if !st.Mode().IsRegular() {
				return nil, "not a regular file"
			}
			f, err := root.OpenFile(part, os.O_RDONLY|nonblockFlag, 0)
			if err != nil {
				return nil, "file not found"
			}
			got, err := f.Stat()
			if err != nil || !os.SameFile(st, got) {
				f.Close()
				return nil, LinkedPath
			}
			return f, ""
		}
		next, err := root.OpenRoot(part)
		if err != nil {
			return nil, "file not found"
		}
		got, err := next.Stat(".")
		if err != nil || !os.SameFile(st, got) {
			next.Close()
			return nil, LinkedPath
		}
		root.Close()
		root = next
	}
	return nil, "not a regular file"
}
