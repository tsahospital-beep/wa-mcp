// Package accounts connects the bridges of the accounts in one config.json:
// one bridge process per account, each with its own store and API address.
// It asks a running bridge for its login link and status through the local
// API, the same way the MCP server does.
package accounts

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "github.com/glebarez/go-sqlite"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/login"
)

// apiBase is the URL of the API of cfg, with an unspecified host replaced by 127.0.0.1.
func apiBase(cfg *config.Config) (string, error) {
	host, port, err := net.SplitHostPort(cfg.API.Listen)
	if err != nil {
		return "", fmt.Errorf("api.listen: %v", err)
	}
	if common.IsUnspecifiedHost(host) {
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, port) + "/api/v1", nil
}

// call sends one request to the bridge of cfg. It never creates the token file.
func call(ctx context.Context, cfg *config.Config, method, path string, out any) (int, error) {
	base, err := apiBase(cfg)
	if err != nil {
		return 0, err
	}
	addr := cfg.API.Listen
	token, err := api.ReadToken(cfg)
	if err != nil {
		return 0, fmt.Errorf("api token: %v", err)
	}
	if token == "" {
		return 0, fmt.Errorf("%w on %s: no API token yet (the bridge of this account has not started once)", login.ErrNotRunning, addr)
	}
	req, err := http.NewRequestWithContext(ctx, method, base+path, nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	client := http.Client{
		Timeout:       10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, fmt.Errorf("%w on %s: %v", login.ErrNotRunning, addr, err)
	}
	defer resp.Body.Close()
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(out); err != nil {
		return resp.StatusCode, fmt.Errorf("unexpected answer from %s (status %d)", addr, resp.StatusCode)
	}
	return resp.StatusCode, nil
}

// LoginLink asks the running bridge of cfg for a fresh one-time link to its
// login window. Errors wrap login.ErrNotRunning or login.ErrLinked when that is the cause.
func LoginLink(ctx context.Context, cfg *config.Config) (string, error) {
	var out struct {
		URL   string `json:"url"`
		Error string `json:"error"`
	}
	status, err := call(ctx, cfg, http.MethodPost, "/login/url", &out)
	switch {
	case err != nil:
		return "", err
	case status == http.StatusConflict:
		return "", fmt.Errorf("%w: %s", login.ErrLinked, out.Error)
	case status != http.StatusOK || out.URL == "":
		if out.Error == "" {
			out.Error = fmt.Sprintf("status %d", status)
		}
		return "", errors.New(out.Error)
	}
	if u, err := url.Parse(out.URL); err != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return "", errors.New("the bridge answered with an unusable link")
	}
	return out.URL, nil
}

// WithAccounts adds the accounts of cfg to the login window options: with
// several accounts the window shows the account name and can open the login
// window of another account. With one account opts is returned unchanged.
func WithAccounts(cfg *config.Config, opts login.Options) login.Options {
	if names := cfg.AccountNames(); len(names) > 1 {
		opts.Account = cfg.Account
		opts.Accounts = names
		opts.OpenAccount = func(ctx context.Context, name string) (string, error) {
			other, err := config.LoadAccount(cfg.File, name)
			if err != nil {
				return "", err
			}
			return LoginLink(ctx, other)
		}
	}
	return opts
}

// Info is the status of one account for `wa-bridge accounts`.
type Info struct {
	Name      string `json:"name"`
	StoreDir  string `json:"store_dir"`
	Listen    string `json:"api_listen"`
	Running   bool   `json:"running"`
	Connected bool   `json:"connected"`
	Linked    bool   `json:"linked"`
	Number    string `json:"number,omitempty"`
	Note      string `json:"note,omitempty"`
}

// Inspect reports the status of the account of cfg: from its bridge when it
// runs, otherwise from its session file (opened read-only).
func Inspect(ctx context.Context, cfg *config.Config) Info {
	info := Info{Name: cfg.Account, StoreDir: relative(cfg.Dir, cfg.StoreDir), Listen: cfg.API.Listen}
	var st struct {
		Connected bool   `json:"connected"`
		LoggedIn  bool   `json:"logged_in"`
		Account   string `json:"account"`
		Error     string `json:"error"`
	}
	status, err := call(ctx, cfg, http.MethodGet, "/status", &st)
	switch {
	case err == nil && status == http.StatusOK:
		info.Running, info.Connected, info.Linked, info.Number = true, st.Connected, st.LoggedIn, st.Account
		return info
	case err == nil:
		info.Note = fmt.Sprintf("the API on %s answered %d: %s", cfg.API.Listen, status, st.Error)
	case !errors.Is(err, login.ErrNotRunning):
		info.Note = err.Error()
	}
	info.Linked, info.Number = sessionDevice(cfg.SessionDB())
	return info
}

// sessionDevice reads the linked device from a session database without
// changing it; false when there is none.
func sessionDevice(path string) (bool, string) {
	if st, err := os.Stat(path); err != nil || st.IsDir() {
		return false, ""
	}
	db, err := sql.Open("sqlite", fsutil.SQLiteURI(path)+"?mode=ro&_pragma=busy_timeout(3000)")
	if err != nil {
		return false, ""
	}
	defer db.Close()
	var jid string
	if err := db.QueryRow("SELECT jid FROM whatsmeow_device LIMIT 1").Scan(&jid); err != nil {
		return false, ""
	}
	user, _, _ := strings.Cut(jid, "@")
	user, _, _ = strings.Cut(user, ":") // drop the device part
	return true, common.MaskNumber(user)
}

func relative(base, p string) string {
	if rel, err := filepath.Rel(base, p); err == nil && filepath.IsLocal(rel) {
		return filepath.ToSlash(rel)
	}
	return p
}
