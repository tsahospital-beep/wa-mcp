package config

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// AgentCurrent checks the config and account switch approved by the worker.
// The same revision follows a send into the quiet-hours queue.
func (c *Config) AgentCurrent(revision string) bool {
	if !c.Agent.Enabled || revision == "" {
		return false
	}
	data, err := os.ReadFile(c.File)
	if err != nil {
		return false
	}
	control, err := os.ReadFile(filepath.Join(c.StoreDir, "agent.switch"))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return false
	}
	if len(control) != 0 && !strings.HasPrefix(string(control), "active ") {
		return false
	}
	hash := sha256.New()
	hash.Write(data)
	hash.Write([]byte{0})
	hash.Write(control)
	return hex.EncodeToString(hash.Sum(nil)) == revision
}
