package config

import (
	"fmt"
	"path/filepath"
	"runtime"
	"strings"
)

// DefaultSessionFile is the whatsmeow session database name inside store_dir.
const DefaultSessionFile = "session.db"

// reservedStoreFiles are bridge files that session_file must not name.
var reservedStoreFiles = []string{"wa.db", "insight.db", "api.token", "bridge.lock"}

// validateSessionFile fills the default and accepts a plain file name inside
// store_dir or an absolute path (for example a volume that only the bridge
// mounts). An absolute path must not be inside a folder that files are sent
// from, and it is added to the paths that are never sent.
func (c *Config) validateSessionFile() error {
	name := strings.TrimSpace(c.SessionFile)
	if name == "" {
		name = DefaultSessionFile
	}
	if abs := ExpandHome(name); filepath.IsAbs(abs) {
		return c.useSessionPath(filepath.Clean(abs))
	}
	if name != filepath.Base(name) || strings.ContainsAny(name, `/\:`) || name == "." || name == ".." {
		return fmt.Errorf("session_file: want a file name inside store_dir or an absolute path, got %q", c.SessionFile)
	}
	for _, r := range reservedStoreFiles {
		if strings.EqualFold(name, r) {
			return fmt.Errorf("session_file: %q is used by the bridge itself", name)
		}
	}
	c.SessionFile = name
	return nil
}

// useSessionPath checks an absolute session path and denies it for sending.
func (c *Config) useSessionPath(path string) error {
	if filepath.Base(path) == "" || strings.HasSuffix(path, string(filepath.Separator)) {
		return fmt.Errorf("session_file: %q is a folder, want a file", path)
	}
	for _, dir := range append([]string{c.DownloadsDir, c.ExportsDir}, c.Sending.AllowedMediaDirs...) {
		if dir != "" && insideDir(dir, path) {
			return fmt.Errorf("session_file: %s is inside %s, a folder that files are sent or read from", path, dir)
		}
	}
	c.SessionFile = path
	for _, p := range []string{path, path + "-wal", path + "-shm"} {
		if !contains(c.Sending.DeniedPaths, p) {
			c.Sending.DeniedPaths = append(c.Sending.DeniedPaths, p)
		}
	}
	return nil
}

// insideDir reports whether path is dir or below it (case-insensitive on Windows and macOS).
func insideDir(dir, path string) bool {
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		dir, path = strings.ToLower(dir), strings.ToLower(path)
	}
	rel, err := filepath.Rel(dir, path)
	return err == nil && !filepath.IsAbs(rel) && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// SetSessionFile replaces session_file after loading (for embedders) and checks it.
func (c *Config) SetSessionFile(name string) error {
	old := c.SessionFile
	c.SessionFile = name
	if err := c.validateSessionFile(); err != nil {
		c.SessionFile = old
		return err
	}
	return nil
}

// UnknownKeyWarning is the warning Load records for a key no setting uses.
func UnknownKeyWarning(file, key string) string {
	return fmt.Sprintf("config %s: unknown key %q is ignored", file, key)
}
