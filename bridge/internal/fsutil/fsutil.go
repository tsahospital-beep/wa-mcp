// Package fsutil creates files and folders readable only by the current user
// (0700/0600 on Unix, a protected owner and SYSTEM only ACL on Windows) and
// provides an exclusive lock file.
package fsutil

import (
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// ErrLocked is returned by Lock when another process holds the lock.
var ErrLocked = errors.New("locked by another process")

// MkdirPrivate creates dir and missing parents and restricts the folders it
// created. Folders that already exist keep their permissions: a downloads
// folder or a target folder chosen by the user is not changed.
func MkdirPrivate(dir string) error {
	if dir == "" {
		return os.Mkdir(dir, 0o700)
	}
	dir = filepath.Clean(dir)
	if st, err := os.Stat(dir); err == nil && st.IsDir() {
		return nil
	}
	if parent := filepath.Dir(dir); parent != dir {
		if err := MkdirPrivate(parent); err != nil {
			return err
		}
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		if errors.Is(err, os.ErrExist) {
			if st, statErr := os.Stat(dir); statErr == nil && st.IsDir() {
				return nil
			}
		}
		return err
	}
	return Restrict(dir)
}

// MkdirOwned creates dir like MkdirPrivate and restricts dir itself even when
// it existed. It is for folders that belong to the bridge, such as store_dir.
func MkdirOwned(dir string) error {
	if err := MkdirPrivate(dir); err != nil {
		return err
	}
	return Restrict(dir)
}

// CreateTemp creates a private temporary file in dir.
func CreateTemp(dir, pattern string) (*os.File, error) {
	if err := MkdirPrivate(dir); err != nil {
		return nil, err
	}
	f, err := os.CreateTemp(dir, pattern)
	if err != nil {
		return nil, err
	}
	if err := restrictFile(f.Name()); err != nil {
		f.Close()
		os.Remove(f.Name())
		return nil, err
	}
	return f, nil
}

// WriteFile writes data to path through a private temporary file and a rename.
func WriteFile(path string, data []byte) error {
	f, err := CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	if err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}

// Restrict limits a file or folder to the current user.
func Restrict(path string) error {
	st, err := os.Stat(path)
	if err != nil {
		return err
	}
	if st.IsDir() {
		return restrictDir(path)
	}
	return restrictFile(path)
}

// RestrictTree applies Restrict to root and everything below it.
func RestrictTree(root string) error {
	return filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		return Restrict(p)
	})
}

// Inside reports whether path is dir itself or below it. Both should be
// absolute; comparison ignores case on Windows and macOS.
func Inside(dir, path string) bool {
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		dir, path = strings.ToLower(dir), strings.ToLower(path)
	}
	rel, err := filepath.Rel(dir, path)
	if err != nil || filepath.IsAbs(rel) {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// Resolve returns the absolute path with symlinks resolved as far as the path exists.
func Resolve(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return filepath.Clean(path)
	}
	if real, err := filepath.EvalSymlinks(abs); err == nil {
		return real
	}
	// Resolve the existing parent so a missing file still compares correctly.
	if parent, err := filepath.EvalSymlinks(filepath.Dir(abs)); err == nil {
		return filepath.Join(parent, filepath.Base(abs))
	}
	return abs
}

// InsideHome reports whether path is inside the user's home folder.
func InsideHome(path string) bool {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return true
	}
	return Inside(Resolve(home), Resolve(path))
}

// SameAsAny reports whether st is the same file as one of paths or as a
// regular file directly inside a folder in paths (hard links and other
// names of a protected file are caught this way).
func SameAsAny(st os.FileInfo, paths []string) bool {
	for _, p := range paths {
		if p == "" {
			continue
		}
		pst, err := os.Stat(p)
		if err != nil {
			continue
		}
		if !pst.IsDir() {
			if os.SameFile(st, pst) {
				return true
			}
			continue
		}
		entries, err := os.ReadDir(p)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if e.Type().IsRegular() {
				if est, err := os.Stat(filepath.Join(p, e.Name())); err == nil && os.SameFile(st, est) {
					return true
				}
			}
		}
	}
	return false
}

// Publish moves a completed temporary file from base into a child folder.
// All path resolution for the rename stays inside the configured output root.
func Publish(base, temp, target string) error {
	root, err := os.OpenRoot(base)
	if err != nil {
		return err
	}
	defer root.Close()
	rel, err := filepath.Rel(base, target)
	if err != nil || !filepath.IsLocal(rel) || filepath.Clean(filepath.Dir(temp)) != filepath.Clean(base) {
		return errors.New("invalid output path")
	}
	dir := filepath.Dir(rel)
	if err := root.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return root.Rename(filepath.Base(temp), rel)
}

// SQLiteURI encodes the file name separately from SQLite connection options.
func SQLiteURI(path string) string {
	abs, err := filepath.Abs(path)
	if err == nil {
		path = abs
	}
	p := filepath.ToSlash(path)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	return (&url.URL{Scheme: "file", Path: p}).String()
}
