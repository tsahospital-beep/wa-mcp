package keystore

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// dpapiBackend keeps each item as a DPAPI blob in the store folder. DPAPI in
// user scope ties the blob to this Windows user on this computer: a copy of
// the folder opened by another user or on another machine cannot unwrap it.
type dpapiBackend struct {
	dir string
}

// DPAPIFile is the file name of an item inside the store folder.
func DPAPIFile(item string) string {
	if item == ItemStoreKey {
		return "key.dpapi"
	}
	return strings.ReplaceAll(item, "-", ".") + ".dpapi"
}

// DPAPIEntropy is the extra secret mixed into each blob (shared with Python).
func DPAPIEntropy(item string) []byte { return []byte("wa-mcp/" + item) }

func (d dpapiBackend) Name() string { return "dpapi" }

func (d dpapiBackend) path(item string) string { return filepath.Join(d.dir, DPAPIFile(item)) }

func (d dpapiBackend) Describe(item string) string {
	return "Windows DPAPI (" + d.path(item) + ")"
}

func (d dpapiBackend) Get(item string) ([]byte, error) {
	blob, err := os.ReadFile(d.path(item))
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	out, err := unprotect(blob, DPAPIEntropy(item))
	if err != nil {
		if errors.Is(err, ErrUnavailable) {
			return nil, err
		}
		return nil, fmt.Errorf("windows DPAPI could not open %s: it was made by another Windows user or on another computer, or it is damaged: %w",
			d.path(item), err)
	}
	return out, nil
}

func (d dpapiBackend) Put(item string, secret []byte) error {
	blob, err := protect(secret, DPAPIEntropy(item))
	if err != nil {
		return err
	}
	return fsutil.WriteFile(d.path(item), blob)
}

func (d dpapiBackend) Delete(item string) error {
	err := os.Remove(d.path(item))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}
