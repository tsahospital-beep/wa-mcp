package config

import (
	"encoding/json"
	"fmt"
)

// Encryption modes for wa.db fields.
const (
	ModeKeystore   = "keystore"   // random store key in the OS keystore (default)
	ModePassphrase = "passphrase" // key from a passphrase in an environment variable
	ModeKeyfile    = "keyfile"    // random store key in a file (for containers)
	ModeOff        = "off"        // no field encryption
)

// Encryption configures field encryption of wa.db and where secrets are kept.
type Encryption struct {
	Mode          string `json:"mode"`
	PassphraseEnv string `json:"passphrase_env"`
	KeyFile       string `json:"key_file"`
	// EFS turns on Windows EFS for the store folder (ignored elsewhere).
	EFS bool `json:"efs"`
	// Enabled is the old switch: true without a mode means mode passphrase.
	Enabled bool `json:"enabled,omitempty"`
}

func defaultEncryption() Encryption {
	return Encryption{Mode: ModeKeystore, PassphraseEnv: "WA_MCP_PASSPHRASE", EFS: true}
}

// encryptionCompat maps the old encryption.enabled key onto encryption.mode.
func (c *Config) encryptionCompat(data []byte, file string) {
	var raw struct {
		Encryption struct {
			Mode    *string `json:"mode"`
			Enabled *bool   `json:"enabled"`
		} `json:"encryption"`
	}
	if json.Unmarshal(data, &raw) != nil || raw.Encryption.Enabled == nil {
		return
	}
	switch {
	case raw.Encryption.Mode != nil:
		c.Warnings = append(c.Warnings, fmt.Sprintf("config %s: encryption.enabled is ignored because encryption.mode is set", file))
	case *raw.Encryption.Enabled:
		c.Encryption.Mode = ModePassphrase
		c.Warnings = append(c.Warnings, fmt.Sprintf("config %s: encryption.enabled is replaced by encryption.mode; using mode passphrase", file))
	default:
		c.Warnings = append(c.Warnings, fmt.Sprintf("config %s: encryption.enabled=false is no longer used; "+
			"the default mode keystore applies (set encryption.mode to off to keep wa.db unencrypted)", file))
	}
}

func (c *Config) resolveEncryption() {
	if c.Encryption.KeyFile != "" {
		c.Encryption.KeyFile = c.abs(c.Encryption.KeyFile)
		c.Sending.DeniedPaths = append(c.Sending.DeniedPaths, c.Encryption.KeyFile)
	}
	c.denyAllStores() // denied.go
}

func (e *Encryption) validate() error {
	switch e.Mode {
	case ModeKeystore, ModeOff:
	case ModePassphrase:
		if e.PassphraseEnv == "" {
			return fmt.Errorf("encryption.passphrase_env is empty")
		}
	case ModeKeyfile:
		if e.KeyFile == "" {
			return fmt.Errorf("encryption.key_file is empty (needed for mode keyfile)")
		}
	default:
		return fmt.Errorf("encryption.mode: want keystore, passphrase, keyfile or off, got %q", e.Mode)
	}
	return nil
}

// Encrypted reports whether field encryption is wanted.
func (e Encryption) Encrypted() bool { return e.Mode != ModeOff }
