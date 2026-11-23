// Package keystore keeps small secrets of one store folder (the wa.db store
// key and the API token) in the operating system's keystore, bound to the
// current user: Windows DPAPI (a wrapped file in the store folder), the macOS
// Keychain (the security tool) or the Secret Service on Linux (the
// secret-tool tool). It also reads key files for the keyfile mode.
//
// The Python package reads the same items (wa_mcp.keystore).
package keystore

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/crypt"
)

// Items kept per store folder.
const (
	ItemStoreKey = "store-key"
	ItemAPIToken = "api-token"
)

// Service is the keychain service and the secret-tool "service" attribute.
const Service = "wa-mcp"

// IDFile holds the random id of a store folder; keychain and Secret Service
// items are named after it, so a moved folder keeps its items. It is not secret.
const IDFile = "keystore.id"

var (
	// ErrNotFound means the backend works but holds no such item.
	ErrNotFound = errors.New("not found in the keystore")
	// ErrUnavailable means there is no usable keystore on this system.
	ErrUnavailable = errors.New("no OS keystore is available")
)

// Backend stores secrets for one store folder.
type Backend interface {
	// Name is "dpapi", "keychain" or "secret-service".
	Name() string
	// Describe names where an item lives, for log lines (never the secret).
	Describe(item string) string
	// Get returns the secret, ErrNotFound when it is missing, or another error.
	Get(item string) ([]byte, error)
	// Put stores or replaces the secret.
	Put(item string, secret []byte) error
	// Delete removes the item; a missing item is not an error.
	Delete(item string) error
}

// ForStore returns the keystore of this platform for the store folder dir.
var ForStore = platformBackend

// Available probes the backend: nil when it can read items (even if it holds none).
func Available(b Backend) error {
	_, err := b.Get(ItemStoreKey)
	if err == nil || errors.Is(err, ErrNotFound) {
		return nil
	}
	return err
}

// StoreKey returns the 32-byte store key. With create, a missing key is
// generated, stored and read back before it is returned.
func StoreKey(b Backend, create bool) ([]byte, error) {
	k, err := b.Get(ItemStoreKey)
	if err == nil {
		if len(k) != crypt.StoreKeyLen {
			return nil, fmt.Errorf("the store key in %s is damaged", b.Describe(ItemStoreKey))
		}
		return k, nil
	}
	if !errors.Is(err, ErrNotFound) || !create {
		return nil, err
	}
	k, err = crypt.NewStoreKey()
	if err != nil {
		return nil, err
	}
	if err := b.Put(ItemStoreKey, k); err != nil {
		return nil, err
	}
	got, err := b.Get(ItemStoreKey)
	if err != nil {
		return nil, fmt.Errorf("reading the new store key back from %s: %w", b.Describe(ItemStoreKey), err)
	}
	if !bytes.Equal(got, k) {
		return nil, fmt.Errorf("%s did not keep the new store key", b.Describe(ItemStoreKey))
	}
	return k, nil
}

// maxSecretFile bounds what ReadKeyFile and ReadSecretFile read.
const maxSecretFile = 4096

// ReadSecretFile reads a small secret file (a key or a passphrase, for
// example a Docker secret). It must be a regular file of at most 4 KiB that
// only the current user can read: mode 0600 or 0400 on macOS and Linux, an
// access list with the user and SYSTEM only on Windows.
func ReadSecretFile(path string) ([]byte, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|secretOpenFlags, 0)
	if err != nil {
		return nil, fmt.Errorf("secret file: %w", err)
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("secret file: %w", err)
	}
	if !st.Mode().IsRegular() {
		return nil, fmt.Errorf("secret file %s is not a regular file", path)
	}
	if st.Size() > maxSecretFile {
		return nil, fmt.Errorf("secret file %s is larger than %d bytes", path, maxSecretFile)
	}
	if err := checkPrivate(f); err != nil {
		return nil, fmt.Errorf("secret file %w", err)
	}
	data, err := io.ReadAll(io.LimitReader(f, maxSecretFile+1))
	if err != nil {
		return nil, fmt.Errorf("secret file: %w", err)
	}
	if len(data) > maxSecretFile {
		return nil, fmt.Errorf("secret file %s is larger than %d bytes", path, maxSecretFile)
	}
	return data, nil
}

// ReadKeyFile reads a 32-byte store key from path (see ReadSecretFile for the
// checks): 64 hex characters, base64 or 32 raw bytes.
func ReadKeyFile(path string) ([]byte, error) {
	data, err := ReadSecretFile(path)
	if err != nil {
		return nil, fmt.Errorf("key file: %w", err)
	}
	k, err := ParseKey(data)
	if err != nil {
		return nil, fmt.Errorf("key file %s: %w", path, err)
	}
	return k, nil
}

// ReadPassphraseFile reads a passphrase from a secret file; one trailing line break is removed.
func ReadPassphraseFile(path string) (string, error) {
	data, err := ReadSecretFile(path)
	if err != nil {
		return "", err
	}
	// One trailing LF or CRLF goes, nothing else (the same rule as wa_mcp.secretfile).
	text := string(data)
	if t, ok := strings.CutSuffix(text, "\n"); ok {
		text = strings.TrimSuffix(t, "\r")
	}
	if text == "" {
		return "", fmt.Errorf("secret file %s is empty", path)
	}
	return text, nil
}

// ParseKey accepts 32 raw bytes, 64 hex characters or base64 of 32 bytes.
func ParseKey(data []byte) ([]byte, error) {
	if len(data) == crypt.StoreKeyLen {
		return append([]byte(nil), data...), nil
	}
	text := strings.TrimSpace(string(data))
	if len(text) == 2*crypt.StoreKeyLen {
		if k, err := hex.DecodeString(text); err == nil {
			return k, nil
		}
	}
	if k, err := base64.StdEncoding.DecodeString(text); err == nil && len(k) == crypt.StoreKeyLen {
		return k, nil
	}
	return nil, errors.New("want 32 bytes as 64 hex characters, base64 or raw bytes")
}
