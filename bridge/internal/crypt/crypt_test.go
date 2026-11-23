package crypt

import (
	"bytes"
	"encoding/base64"
	"strings"
	"testing"
)

func newTestSealer(t *testing.T, pass string, salt []byte) *Sealer {
	t.Helper()
	s, err := NewSealer(DeriveKey(pass, salt))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestRoundTrip(t *testing.T) {
	salt, err := NewSalt()
	if err != nil {
		t.Fatal(err)
	}
	s := newTestSealer(t, "correct horse", salt)

	for _, text := range []string{"hello Bob", "Book club at 7", strings.Repeat("x", 5000)} {
		sealed, err := s.SealString(text)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasPrefix(sealed, Prefix) {
			t.Fatalf("missing prefix: %q", sealed)
		}
		raw, err := base64.StdEncoding.DecodeString(sealed[len(Prefix):])
		if err != nil {
			t.Fatal(err)
		}
		if len(raw) != 12+len(text)+16 {
			t.Fatalf("unexpected sealed length %d", len(raw))
		}
		got, err := s.OpenString(sealed)
		if err != nil || got != text {
			t.Fatalf("round trip: %q, %v", got, err)
		}
	}

	empty, _ := s.SealString("")
	if empty != "" {
		t.Fatalf("empty string must stay empty, got %q", empty)
	}
	plain, err := s.OpenString("not sealed")
	if err != nil || plain != "not sealed" {
		t.Fatalf("plain values must pass through: %q %v", plain, err)
	}

	key := []byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	blob, err := s.SealBlob(key)
	if err != nil || !bytes.HasPrefix(blob, []byte(Prefix)) {
		t.Fatalf("blob: %q %v", blob, err)
	}
	back, err := s.OpenBlob(blob)
	if err != nil || !bytes.Equal(back, key) {
		t.Fatalf("blob round trip: %v %v", back, err)
	}
}

func TestWrongPassphrase(t *testing.T) {
	salt, _ := NewSalt()
	good := newTestSealer(t, "alice-secret", salt)
	bad := newTestSealer(t, "bob-guess", salt)

	check, err := good.MakeCheck()
	if err != nil {
		t.Fatal(err)
	}
	if !good.VerifyCheck(check) {
		t.Fatal("right passphrase rejected")
	}
	if bad.VerifyCheck(check) {
		t.Fatal("wrong passphrase accepted")
	}
	sealed, _ := good.SealString("secret note")
	if _, err := bad.OpenString(sealed); err == nil {
		t.Fatal("wrong key opened the value")
	}

	otherSalt, _ := NewSalt()
	if newTestSealer(t, "alice-secret", otherSalt).VerifyCheck(check) {
		t.Fatal("different salt accepted")
	}
}

func TestDamagedValue(t *testing.T) {
	salt, _ := NewSalt()
	s := newTestSealer(t, "pw", salt)
	sealed, _ := s.SealString("hello")
	raw, _ := base64.StdEncoding.DecodeString(sealed[len(Prefix):])
	raw[len(raw)-1] ^= 0xff
	tampered := Prefix + base64.StdEncoding.EncodeToString(raw)
	if _, err := s.OpenString(tampered); err == nil {
		t.Fatal("tampered value accepted")
	}
	if _, err := s.OpenString(Prefix + "%%%"); err == nil {
		t.Fatal("bad base64 accepted")
	}
	if _, err := s.OpenString(Prefix + "AAAA"); err == nil {
		t.Fatal("short value accepted")
	}
}

func TestSubkey(t *testing.T) {
	salt, _ := NewSalt()
	a := newTestSealer(t, "pw", salt)
	b := newTestSealer(t, "pw", salt)
	if !bytes.Equal(a.Subkey("outbox"), b.Subkey("outbox")) || len(a.Subkey("outbox")) != 32 {
		t.Fatal("subkey must be stable for the same key")
	}
	if bytes.Equal(a.Subkey("outbox"), a.Subkey("other")) {
		t.Fatal("labels must give different keys")
	}
	other, _ := NewSalt()
	if bytes.Equal(a.Subkey("outbox"), newTestSealer(t, "pw", other).Subkey("outbox")) {
		t.Fatal("different store keys must give different subkeys")
	}
}
