package crypt

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type v2Vectors struct {
	KeyHex   string `json:"key_hex"`
	V1Sealed string `json:"v1_sealed"`
	V1Plain  string `json:"v1_plain"`
	Vectors  []struct {
		Table    string `json:"table"`
		Column   string `json:"column"`
		Conv     string `json:"conv"`
		MsgID    string `json:"msg_id"`
		Source   string `json:"source"`
		AADHex   string `json:"aad_hex"`
		Plain    string `json:"plain"`
		PlainHex string `json:"plain_hex"`
		Sealed   string `json:"sealed"`
	} `json:"vectors"`
}

// The vectors are shared with the Python reader.
func TestAADVectorsAndBinding(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "crypt_vectors_v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v v2Vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	key, _ := hex.DecodeString(v.KeyHex)
	s, err := NewSealer(key)
	if err != nil {
		t.Fatal(err)
	}
	for _, x := range v.Vectors {
		var extra []string
		if x.Source != "" {
			extra = []string{x.Source}
		}
		aad := AAD(x.Table, x.Column, x.Conv, x.MsgID, extra...)
		if hex.EncodeToString(aad) != x.AADHex {
			t.Fatalf("%s.%s: aad %x", x.Table, x.Column, aad)
		}
		want := []byte(x.Plain)
		if x.PlainHex != "" {
			want, _ = hex.DecodeString(x.PlainHex)
		}
		got, err := s.OpenAAD(x.Sealed, aad)
		if err != nil || !bytes.Equal(got, want) {
			t.Fatalf("%s.%s: %q %v", x.Table, x.Column, got, err)
		}
		// The same value in another row, column or table does not open.
		for _, other := range [][]byte{
			AAD(x.Table, x.Column, x.Conv, x.MsgID+"x"),
			AAD(x.Table, "other", x.Conv, x.MsgID),
			AAD("other", x.Column, x.Conv, x.MsgID),
		} {
			if _, err := s.OpenAAD(x.Sealed, other); !errors.Is(err, ErrBadSealed) {
				t.Fatalf("%s.%s opened with another AAD: %v", x.Table, x.Column, err)
			}
		}
		if _, err := s.Open(x.Sealed); err == nil {
			t.Fatal("a wae2 value opened without its AAD")
		}
	}
	if n := len(v.Vectors); n != 12 {
		t.Fatalf("want one vector per sealed column, got %d", n)
	}
	if string(AAD("derived_text", "content", "c", "m", "transcript")) != "derived_text|content|c|m|transcript" {
		t.Fatal("five-part AAD")
	}
	// Old values stay readable; the AAD does not matter for them.
	if got, err := s.OpenAAD(v.V1Sealed, AAD("chat_messages", "body", "x", "y")); err != nil || string(got) != v.V1Plain {
		t.Fatalf("v1: %q %v", got, err)
	}
	fresh, _ := s.SealAAD([]byte("round trip"), AAD("t", "c", "a", "b"))
	if !strings.HasPrefix(fresh, PrefixV2) || !IsSealed(fresh) {
		t.Fatalf("prefix %q", fresh)
	}
}
