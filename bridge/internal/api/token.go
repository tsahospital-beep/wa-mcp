package api

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/keystore"
)

// tokenBackend is the OS keystore that holds the API token in mode
// keystore, nil in the other modes, where the token is in store/api.token.
// Tests replace it.
var tokenBackend = func(cfg *config.Config) keystore.Backend {
	if cfg.Encryption.Mode != config.ModeKeystore {
		return nil
	}
	return keystore.ForStore(cfg.StoreDir)
}

// A keystore that does not answer is asked again a few times (a keyring can
// still be locked right after login, D-Bus can start late).
var (
	tokenTries = 3
	tokenWait  = 500 * time.Millisecond
)

// tokenKeystore returns the keystore for the token, nil when the token is in
// the plain file, or an error when mode keystore is set and the keystore does
// not answer: a silent switch to a new plain token would leave the clients
// with the old one. api.token_file_fallback allows the plain file instead.
func tokenKeystore(cfg *config.Config) (keystore.Backend, error) {
	b := tokenBackend(cfg)
	if b == nil {
		return nil, nil
	}
	var err error
	for try := 0; try < tokenTries; try++ {
		if err = keystore.Available(b); err == nil {
			return b, nil
		}
		if try+1 < tokenTries {
			time.Sleep(tokenWait)
		}
	}
	if cfg.API.TokenFileFallback {
		return nil, nil
	}
	return nil, fmt.Errorf("api token: %v. The token is kept in %s in mode keystore. Fix the keystore, or set api.token, "+
		"or api.token_file_fallback=true to keep it in %s instead", err, b.Describe(keystore.ItemAPIToken), cfg.TokenFile())
}

// getKeystoreToken returns the token kept in b ("" when there is none).
func getKeystoreToken(b keystore.Backend) (string, error) {
	v, err := b.Get(keystore.ItemAPIToken)
	if errors.Is(err, keystore.ErrNotFound) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("api token: %w", err)
	}
	if t := strings.TrimSpace(string(v)); len(t) >= MinTokenLen {
		return t, nil
	}
	return "", nil
}

// loadKeystoreToken returns the token from the keystore. A token found in the
// plain file is moved into the keystore; without either a new one is made.
// The plain file is wiped afterwards.
func loadKeystoreToken(cfg *config.Config, b keystore.Backend) (string, error) {
	stored, err := getKeystoreToken(b)
	if err != nil {
		return "", err
	}
	if stored == "" {
		tok, err := ReadToken(cfg) // the plain file, if any
		if err != nil {
			return "", err
		}
		if tok == "" {
			buf := make([]byte, 32)
			if _, err := rand.Read(buf); err != nil {
				return "", err
			}
			tok = hex.EncodeToString(buf)
		}
		if err := b.Put(keystore.ItemAPIToken, []byte(tok)); err != nil {
			return "", fmt.Errorf("api token: %w", err)
		}
		if again, err := getKeystoreToken(b); err != nil || again != tok {
			return "", fmt.Errorf("api token: %s did not keep the token", b.Describe(keystore.ItemAPIToken))
		}
		stored = tok
	}
	if err := fsutil.Wipe(cfg.TokenFile()); err != nil {
		return "", fmt.Errorf("api token: removing the plain copy: %w", err)
	}
	return stored, nil
}

// TokenSource names where the token comes from, for the start log (never the token).
func TokenSource(cfg *config.Config) string {
	if strings.TrimSpace(cfg.API.Token) != "" {
		return "from config"
	}
	if b, err := tokenKeystore(cfg); err == nil && b != nil {
		return b.Describe(keystore.ItemAPIToken)
	}
	return cfg.TokenFile()
}
