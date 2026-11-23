// Package crypt seals single database fields with AES-256-GCM.
// The key comes from a passphrase via Argon2id, or from a random 32-byte
// store key (OS keystore or key file) via HKDF-SHA256.
//
// Sealed value formats: "wae1:" + base64(nonce(12) || ciphertext+tag), and
// "wae2:" with the same layout and associated data that names the place of
// the value (see AAD), so a value moved to another row or column does not open.
package crypt

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"strings"

	"golang.org/x/crypto/argon2"
)

// Prefix marks a sealed value without associated data (still read; used for meta.crypt_check).
const Prefix = "wae1:"

// PrefixV2 marks a value sealed with associated data (all new field values).
const PrefixV2 = "wae2:"

// CheckPlaintext is sealed into meta.crypt_check to verify a passphrase.
const CheckPlaintext = "wa-mcp"

// Argon2id parameters (shared with the Python reader).
const (
	argonTime    = 3
	argonMemory  = 64 * 1024 // KiB
	argonThreads = 2
	keyLen       = 32
	SaltLen      = 16
	nonceLen     = 12
)

var (
	ErrNotSealed  = errors.New("value is not sealed")
	ErrBadSealed  = errors.New("sealed value is damaged or the key is wrong")
	ErrShortInput = errors.New("sealed value is too short")
)

// NewSalt returns random salt bytes for DeriveKey.
func NewSalt() ([]byte, error) {
	salt := make([]byte, SaltLen)
	if _, err := rand.Read(salt); err != nil {
		return nil, err
	}
	return salt, nil
}

// DeriveKey turns a passphrase into a 32-byte key.
func DeriveKey(passphrase string, salt []byte) []byte {
	return argon2.IDKey([]byte(passphrase), salt, argonTime, argonMemory, argonThreads, keyLen)
}

// FieldKeyInfo is the HKDF info string for field keys made from a store key.
const FieldKeyInfo = "wa-mcp/wa.db/fields/v1"

// StoreKeyLen is the size of a store key kept in the OS keystore or a key file.
const StoreKeyLen = 32

// FieldKey derives the field key from a random 32-byte store key with
// HKDF-SHA256 (salt from meta.crypt_salt). The Python reader does the same.
func FieldKey(storeKey, salt []byte) ([]byte, error) {
	if len(storeKey) != StoreKeyLen {
		return nil, errors.New("store key must be 32 bytes")
	}
	return hkdf.Key(sha256.New, storeKey, salt, FieldKeyInfo, keyLen)
}

// NewStoreKey returns 32 random bytes for a new store key.
func NewStoreKey() ([]byte, error) {
	k := make([]byte, StoreKeyLen)
	if _, err := rand.Read(k); err != nil {
		return nil, err
	}
	return k, nil
}

// Sealer encrypts and decrypts field values with one key.
type Sealer struct {
	aead cipher.AEAD
	key  []byte
}

// NewSealer builds a Sealer from a 32-byte key.
func NewSealer(key []byte) (*Sealer, error) {
	if len(key) != keyLen {
		return nil, errors.New("key must be 32 bytes")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return &Sealer{aead: aead, key: append([]byte(nil), key...)}, nil
}

// Subkey derives a separate 32-byte key for another purpose (HMAC-SHA256 of the label).
func (s *Sealer) Subkey(label string) []byte {
	m := hmac.New(sha256.New, s.key)
	m.Write([]byte("wa-mcp/" + label))
	return m.Sum(nil)
}

// IsSealed reports whether v carries a sealed-value prefix.
func IsSealed(v string) bool { return strings.HasPrefix(v, Prefix) || strings.HasPrefix(v, PrefixV2) }

// AAD is the associated data of a "wae2:" value: the UTF-8 bytes of
// table + "|" + column + "|" + conv + "|" + msgID, followed by "|" + each
// extra part. Rows without a message use their own key for conv
// (scheduled_messages: target; wa_contacts: jid) and "" for msgID;
// derived_text adds its source, since one message can have several rows.
func AAD(table, column, conv, msgID string, extra ...string) []byte {
	s := table + "|" + column + "|" + conv + "|" + msgID
	for _, e := range extra {
		s += "|" + e
	}
	return []byte(s)
}

// SealAAD encrypts plain bound to aad and returns the "wae2:" text form.
func (s *Sealer) SealAAD(plain, aad []byte) (string, error) {
	nonce := make([]byte, nonceLen, nonceLen+len(plain)+s.aead.Overhead())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	out := s.aead.Seal(nonce, nonce, plain, aad)
	return PrefixV2 + base64.StdEncoding.EncodeToString(out), nil
}

// OpenAAD decrypts a "wae2:" value bound to aad, or a "wae1:" value (aad is not used for it).
func (s *Sealer) OpenAAD(v string, aad []byte) ([]byte, error) {
	switch {
	case strings.HasPrefix(v, PrefixV2):
	case strings.HasPrefix(v, Prefix):
		aad = nil
	default:
		return nil, ErrNotSealed
	}
	raw, err := base64.StdEncoding.DecodeString(v[len(Prefix):])
	if err != nil {
		return nil, ErrBadSealed
	}
	if len(raw) < nonceLen+s.aead.Overhead() {
		return nil, ErrShortInput
	}
	plain, err := s.aead.Open(nil, raw[:nonceLen], raw[nonceLen:], aad)
	if err != nil {
		return nil, ErrBadSealed
	}
	return plain, nil
}

// Seal encrypts plain and returns the prefixed text form.
func (s *Sealer) Seal(plain []byte) (string, error) {
	nonce := make([]byte, nonceLen, nonceLen+len(plain)+s.aead.Overhead())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	out := s.aead.Seal(nonce, nonce, plain, nil)
	return Prefix + base64.StdEncoding.EncodeToString(out), nil
}

// Open decrypts a "wae1:" value (a "wae2:" value needs OpenAAD).
func (s *Sealer) Open(v string) ([]byte, error) { return s.OpenAAD(v, nil) }

// SealString seals text; the empty string stays empty.
func (s *Sealer) SealString(plain string) (string, error) {
	if plain == "" {
		return "", nil
	}
	return s.Seal([]byte(plain))
}

// OpenString opens a sealed value; values without the prefix pass through,
// so rows written before encryption was enabled stay readable.
func (s *Sealer) OpenString(v string) (string, error) {
	if !IsSealed(v) {
		return v, nil
	}
	b, err := s.Open(v)
	return string(b), err
}

// SealBlob seals binary data; the stored blob is the bytes of the prefixed text.
func (s *Sealer) SealBlob(plain []byte) ([]byte, error) {
	if len(plain) == 0 {
		return plain, nil
	}
	v, err := s.Seal(plain)
	return []byte(v), err
}

// OpenBlob reverses SealBlob; blobs without the prefix pass through.
func (s *Sealer) OpenBlob(b []byte) ([]byte, error) {
	if !IsSealed(string(b)) {
		return b, nil
	}
	return s.Open(string(b))
}

// MakeCheck returns the value stored in meta.crypt_check.
func (s *Sealer) MakeCheck() (string, error) { return s.SealString(CheckPlaintext) }

// VerifyCheck reports whether check was sealed with this key.
func (s *Sealer) VerifyCheck(check string) bool {
	v, err := s.Open(check)
	return err == nil && string(v) == CheckPlaintext
}
