package crypt

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The vectors are shared with the Python reader (sdk/python/tests/test_keystore.py).
func TestFieldKeyMatchesSharedVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "keystore_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	storeKey, _ := hex.DecodeString(v["store_key_hex"].(string))
	salt, _ := base64.StdEncoding.DecodeString(v["salt_b64"].(string))
	if v["info"] != FieldKeyInfo {
		t.Fatalf("info %q", v["info"])
	}
	fk, err := FieldKey(storeKey, salt)
	if err != nil || hex.EncodeToString(fk) != v["field_key_hex"] {
		t.Fatalf("field key %x %v", fk, err)
	}
	s, err := NewSealer(fk)
	if err != nil {
		t.Fatal(err)
	}
	if !s.VerifyCheck(v["check"].(string)) {
		t.Fatal("check value")
	}
	if got, err := s.OpenString(v["sealed"].(string)); err != nil || got != v["sealed_plain"] {
		t.Fatalf("sealed %q %v", got, err)
	}
	want, _ := hex.DecodeString(v["blob_plain_hex"].(string))
	if got, err := s.OpenBlob([]byte(v["blob"].(string))); err != nil || !bytes.Equal(got, want) {
		t.Fatalf("blob %x %v", got, err)
	}
	if _, err := FieldKey(storeKey[:31], salt); err == nil {
		t.Fatal("short store key accepted")
	}
	other, _ := FieldKey(storeKey, []byte("another salt...."))
	if bytes.Equal(other, fk) {
		t.Fatal("the salt does not change the key")
	}
}
