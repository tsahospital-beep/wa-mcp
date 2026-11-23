package fsutil

import (
	"io/fs"
	"path/filepath"
)

// SweepTemp wipes leftover temporary files below root whose names match one
// of the glob patterns, for example partial downloads after a crash. It
// returns how many it removed; a missing root is not an error.
func SweepTemp(root string, patterns ...string) int {
	n := 0
	filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || !d.Type().IsRegular() {
			return nil
		}
		for _, pat := range patterns {
			if ok, _ := filepath.Match(pat, d.Name()); ok {
				if Wipe(p) == nil {
					n++
				}
				break
			}
		}
		return nil
	})
	return n
}
