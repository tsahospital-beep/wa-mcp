package config

import (
	"errors"
	"strings"
)

// Import tunes the import of exported chat files (wa-bridge import-export, POST /api/v1/import).
type Import struct {
	// AllowedDirs adds folders to downloads_dir and exports_dir, which are always allowed,
	// as places an import file may be read from by path.
	AllowedDirs []string `json:"allowed_dirs"`
	// MaxMB limits the file and, for a zip, its unpacked size.
	MaxMB int `json:"max_mb"`
	// OmittedPhrases adds lines that stand for a media file left out of the export.
	OmittedPhrases []string `json:"omitted_phrases"`
}

func defaultImport() Import {
	return Import{AllowedDirs: []string{}, MaxMB: 1024, OmittedPhrases: []string{}}
}

func (c *Config) resolveImport() {
	var dirs []string
	for _, d := range c.Import.AllowedDirs {
		if d = strings.TrimSpace(d); d != "" {
			dirs = append(dirs, c.abs(d))
		}
	}
	c.Import.AllowedDirs = dirs
	var phrases []string
	for _, p := range c.Import.OmittedPhrases {
		if p = strings.TrimSpace(p); p != "" {
			phrases = append(phrases, p)
		}
	}
	c.Import.OmittedPhrases = phrases
}

func (c *Config) validateImport() error {
	if c.Import.MaxMB <= 0 {
		return errors.New("import.max_mb must be > 0")
	}
	return nil
}

// ImportDirs lists the folders an import file may be read from by path: downloads_dir, exports_dir
// and import.allowed_dirs.
func (c *Config) ImportDirs() []string {
	dirs := []string{c.DownloadsDir, c.ExportsDir}
	for _, d := range c.Import.AllowedDirs {
		if !contains(dirs, d) {
			dirs = append(dirs, d)
		}
	}
	return dirs
}

// MaxImportBytes is the size limit of an import in bytes.
func (c *Config) MaxImportBytes() int64 { return int64(c.Import.MaxMB) << 20 }
