package config

import (
	"encoding/json"
	"fmt"
	"net"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
)

// DefaultAccount is the account that uses the top-level settings. It always exists.
const DefaultAccount = "default"

var accountName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,31}$`)

// Account is one entry of "accounts": the settings that may differ per WhatsApp
// number. Everything else comes from the top level.
type Account struct {
	StoreDir     string       `json:"store_dir"`
	DownloadsDir string       `json:"downloads_dir"`
	ExportsDir   string       `json:"exports_dir"`
	API          AccountAPI   `json:"api"`
	Login        AccountLogin `json:"login"`
	Sending      Sending      `json:"sending"`
	// Webhooks replace the top-level list for this account when the block has them.
	Webhooks []Webhook `json:"webhooks"`

	raw json.RawMessage // the block as written, merged over the inherited values
}

// AccountAPI is the part of "api" an account may change.
type AccountAPI struct {
	Listen string `json:"listen"`
}

// AccountLogin is the part of "login" an account may change.
type AccountLogin struct {
	Port int `json:"port"`
}

// UnmarshalJSON keeps the block so that only the keys it contains replace inherited values.
func (a *Account) UnmarshalJSON(b []byte) error {
	type plain Account
	if err := json.Unmarshal(b, (*plain)(a)); err != nil {
		return err
	}
	a.raw = append(json.RawMessage(nil), b...)
	return nil
}

// AccountNames lists the configured accounts: "default" first, then the others by name.
func (c *Config) AccountNames() []string {
	out := []string{DefaultAccount}
	for name := range c.Accounts {
		if name != DefaultAccount {
			out = append(out, name)
		}
	}
	sort.Strings(out[1:])
	return out
}

func (s Sending) clone() Sending {
	s.PauseBetween = append([]float64(nil), s.PauseBetween...)
	s.AllowedMediaDirs = append([]string(nil), s.AllowedMediaDirs...)
	s.DeniedPaths = append([]string(nil), s.DeniedPaths...)
	return s
}

// accountView is the settings of one account before paths are resolved: the
// top-level values, a subfolder per account for the folders of accounts other
// than "default", and the keys of its block on top.
func (c *Config) accountView(name string) (Account, error) {
	v := Account{
		StoreDir:     c.StoreDir,
		DownloadsDir: c.DownloadsDir,
		ExportsDir:   c.ExportsDir,
		API:          AccountAPI{Listen: c.API.Listen},
		Login:        AccountLogin{Port: c.Login.Port},
		Sending:      c.Sending.clone(),
		Webhooks:     append([]Webhook(nil), c.Webhooks...),
	}
	if name != DefaultAccount {
		v.StoreDir = filepath.Join(v.StoreDir, name)
		v.DownloadsDir = filepath.Join(v.DownloadsDir, name)
		v.ExportsDir = filepath.Join(v.ExportsDir, name)
		v.API.Listen = "" // two bridges cannot share one address
	}
	if block, ok := c.Accounts[name]; ok && len(block.raw) > 0 {
		if err := json.Unmarshal(block.raw, &v); err != nil {
			return v, fmt.Errorf("accounts.%s: %v", name, err)
		}
	}
	return v, nil
}

// selectAccount applies the settings of one account (empty = "default") to c
// after checking that the accounts do not share a store, an API port or a login port.
func (c *Config) selectAccount(name string) error {
	if name == "" {
		name = DefaultAccount
	}
	names := c.AccountNames()
	for _, n := range names {
		if !accountName.MatchString(n) {
			return fmt.Errorf("accounts: %q is not a valid account name (use a-z, 0-9, - and _, at most 32 characters)", n)
		}
	}
	if !contains(names, name) {
		return fmt.Errorf("account %q is not configured (accounts: %s)", name, strings.Join(names, ", "))
	}
	views := make(map[string]Account, len(names))
	for _, n := range names {
		v, err := c.accountView(n)
		if err != nil {
			return err
		}
		views[n] = v
	}
	if err := c.checkAccounts(names, views); err != nil {
		return err
	}
	v := views[name]
	c.Account = name
	c.accountStores = nil
	for _, n := range names {
		c.accountStores = append(c.accountStores, views[n].StoreDir)
	}
	c.StoreDir, c.DownloadsDir, c.ExportsDir = v.StoreDir, v.DownloadsDir, v.ExportsDir
	c.API.Listen = v.API.Listen
	c.Login.Port = v.Login.Port
	c.Sending = v.Sending
	c.Webhooks = v.Webhooks
	return nil
}

func (c *Config) checkAccounts(names []string, views map[string]Account) error {
	if len(names) < 2 {
		return nil
	}
	ports, stores, logins := map[string]string{}, map[string]string{}, map[int]string{}
	for _, n := range names {
		v := views[n]
		if v.API.Listen == "" {
			return fmt.Errorf("accounts.%s.api.listen: set an address for this account, for example 127.0.0.1:8081", n)
		}
		_, port, err := net.SplitHostPort(v.API.Listen)
		if err != nil {
			return fmt.Errorf("accounts.%s.api.listen: %v", n, err)
		}
		if other, ok := ports[port]; ok && port != "0" {
			return fmt.Errorf("accounts.%s.api.listen: port %s is already used by account %s", n, port, other)
		}
		ports[port] = n
		key := filepath.Clean(c.abs(v.StoreDir))
		if runtime.GOOS == "windows" {
			key = strings.ToLower(key)
		}
		if other, ok := stores[key]; ok {
			return fmt.Errorf("accounts.%s.store_dir: %s is already used by account %s; every account needs its own store folder", n, c.abs(v.StoreDir), other)
		}
		stores[key] = n
		if other, ok := logins[v.Login.Port]; ok && v.Login.Port != 0 {
			return fmt.Errorf("accounts.%s.login.port: %d is already used by account %s; use another port or 0", n, v.Login.Port, other)
		}
		logins[v.Login.Port] = n
	}
	return nil
}

// stripAccount turns "accounts.work.sending.x" into "sending.x" for the
// lists of Python-only keys.
func stripAccount(key string) string {
	if rest, ok := strings.CutPrefix(key, "accounts."); ok {
		if _, tail, found := strings.Cut(rest, "."); found {
			return tail
		}
	}
	return key
}
