//go:build windows

package keystore

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDPAPIRoundTrip(t *testing.T) {
	dir := t.TempDir()
	b := ForStore(dir)
	if b.Name() != "dpapi" || Available(b) != nil {
		t.Fatalf("backend %s", b.Name())
	}
	if _, err := StoreKey(b, false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing key: %v", err)
	}
	key, err := StoreKey(b, true)
	if err != nil {
		t.Fatal(err)
	}
	again, err := StoreKey(ForStore(dir), false)
	if err != nil || !bytes.Equal(again, key) {
		t.Fatalf("read back: %v", err)
	}
	wrapped, err := os.ReadFile(filepath.Join(dir, "key.dpapi"))
	if err != nil || bytes.Contains(wrapped, key) {
		t.Fatalf("blob holds the plain key or is missing: %v", err)
	}
	if !strings.Contains(b.Describe(ItemAPIToken), "api.token.dpapi") {
		t.Fatalf("describe: %s", b.Describe(ItemAPIToken))
	}
}

func TestDPAPIOtherUserOrMachineCannotOpen(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	blob, err := protect(secret, DPAPIEntropy(ItemStoreKey))
	if err != nil {
		t.Fatal(err)
	}
	if got, err := unprotect(blob, DPAPIEntropy(ItemStoreKey)); err != nil || !bytes.Equal(got, secret) {
		t.Fatalf("round trip: %v", err)
	}
	// Another user's or machine's master key is simulated by a different entropy.
	if _, err := unprotect(blob, DPAPIEntropy(ItemAPIToken)); err == nil {
		t.Fatal("a blob opened with the wrong entropy")
	}
	damaged := append([]byte(nil), blob...)
	damaged[len(damaged)-5] ^= 0xff
	if _, err := unprotect(damaged, DPAPIEntropy(ItemStoreKey)); err == nil {
		t.Fatal("a damaged blob opened")
	}

	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, DPAPIFile(ItemStoreKey)), damaged, 0o600)
	_, err = ForStore(dir).Get(ItemStoreKey)
	if err == nil || errors.Is(err, ErrNotFound) || !strings.Contains(err.Error(), "another Windows user") {
		t.Fatalf("damaged file: %v", err)
	}
}
