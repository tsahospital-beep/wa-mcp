package config

import (
	"os"
	"path/filepath"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// denyAllStores adds to the paths that are never sent: the store folders of
// every account, the folders of the other accounts (the downloads and
// exports folders of "default" contain them), and the passphrase file named
// in <passphrase_env>_FILE. Key files and an absolute session_file are added
// where they are resolved.
func (c *Config) denyAllStores() {
	deny := func(p string) {
		if p != "" && !contains(c.Sending.DeniedPaths, p) {
			c.Sending.DeniedPaths = append(c.Sending.DeniedPaths, p)
		}
	}
	for _, dir := range c.accountStores {
		if dir != "" {
			deny(c.abs(dir))
		}
	}
	c.Sending.ownMedia = []string{c.DownloadsDir, c.ExportsDir}
	for _, dir := range c.OtherAccountFolders() {
		if strictlyBelow(dir, c.DownloadsDir) || strictlyBelow(dir, c.ExportsDir) {
			c.Sending.foreignParents = append(c.Sending.foreignParents, dir)
		} else {
			deny(dir)
		}
	}
	for _, name := range append(c.secretEnvNames, c.Encryption.PassphraseEnv, "WA_MCP_SERVER_PEPPER") {
		if f := os.Getenv(name + "_FILE"); name != "" && f != "" {
			if path, err := filepath.Abs(f); err == nil {
				deny(path)
			}
		}
	}
}

// OtherAccountFolders returns the store, downloads and exports folders of the
// accounts other than this one, as absolute paths. Work on this account's
// folders (such as the sweep of temporary files) skips them.
func (c *Config) OtherAccountFolders() []string {
	var out []string
	for _, dir := range c.otherFolders {
		if dir != "" {
			out = append(out, c.abs(dir))
		}
	}
	return out
}

func strictlyBelow(parent, child string) bool {
	return fsutil.Inside(parent, child) && !fsutil.Inside(child, parent)
}

// ForeignMedia protects a parent's files while allowing an account's own subtree.
func (s Sending) ForeignMedia(path string) bool {
	for _, parent := range s.foreignParents {
		root := fsutil.Resolve(parent)
		if !fsutil.Inside(root, path) {
			continue
		}
		owned := false
		for _, dir := range s.ownMedia {
			own := fsutil.Resolve(dir)
			if strictlyBelow(root, own) && fsutil.Inside(own, path) {
				owned = true
				break
			}
		}
		if !owned {
			return true
		}
	}
	return false
}
