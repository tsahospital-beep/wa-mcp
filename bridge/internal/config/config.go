// Package config loads config.json, applies defaults and environment overrides.
package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Agent holds the switches needed for live event delivery and call rejection.
type Agent struct {
	Enabled    bool   `json:"enabled"`
	Mode       string `json:"mode"`
	DryRun     bool   `json:"dry_run"`
	AutoReject bool   `json:"auto_reject"`
}

// Config is the part of config.json the bridge uses.
type Config struct {
	Agent        Agent        `json:"agent"`
	StoreDir     string       `json:"store_dir"`
	DownloadsDir string       `json:"downloads_dir"`
	ExportsDir   string       `json:"exports_dir"`
	SessionFile  string       `json:"session_file"`
	API          API          `json:"api"`
	Login        Login        `json:"login"`
	ReadReceipts ReadReceipts `json:"read_receipts"`
	Calls        Calls        `json:"calls"`
	Media        Media        `json:"media"`
	Sending      Sending      `json:"sending"`
	Webhooks     []Webhook    `json:"webhooks"`
	Encryption   Encryption   `json:"encryption"`
	Import       Import       `json:"import"`
	// Accounts adds WhatsApp numbers next to "default"; see accounts.go.
	Accounts map[string]Account `json:"accounts"`

	// Account is the name of the account these settings belong to.
	Account string `json:"-"`
	// File is the config path that was used (it may not exist).
	File string `json:"-"`
	// Dir is the folder relative paths are resolved against.
	Dir string `json:"-"`
	// Warnings lists problems that did not stop loading, such as unknown keys.
	Warnings []string `json:"-"`
	// accountStores are the store folders of all accounts (never sent, see denied.go).
	accountStores  []string
	secretEnvNames []string
	// otherFolders are the store, downloads and exports folders of the other accounts.
	otherFolders []string
}

type API struct {
	Listen string `json:"listen"`
	Token  string `json:"token"`
	// TokenFileFallback keeps the token in store/api.token when mode keystore is set but the keystore fails.
	TokenFileFallback bool `json:"token_file_fallback"`
}

type Login struct {
	DeviceName     string      `json:"device_name"`
	DevicePlatform string      `json:"device_platform"`
	OpenWindow     bool        `json:"open_window"`
	Port           int         `json:"port"`
	Public         LoginPublic `json:"public"` // server mode, see login_public.go
}

type ReadReceipts struct {
	AutoMarkRead  bool `json:"auto_mark_read"`
	AllowMarkRead bool `json:"allow_mark_read"` // the /read endpoint
}

type Calls struct {
	Record bool `json:"record"`
}

type Media struct {
	MaxDownloadMB int `json:"max_download_mb"`
}

type Sending struct {
	Enabled              bool       `json:"enabled"`
	RequireConfirm       bool       `json:"require_confirm"`
	HumanLike            bool       `json:"human_like"`
	MarkReadBeforeReply  bool       `json:"mark_read_before_reply"`
	TypingCharsPerSecond float64    `json:"typing_chars_per_second"`
	TypingMinSeconds     float64    `json:"typing_min_seconds"`
	TypingMaxSeconds     float64    `json:"typing_max_seconds"`
	Jitter               float64    `json:"jitter"`
	PauseBetween         []float64  `json:"pause_between_messages_seconds"`
	QuietHours           QuietHours `json:"quiet_hours"`
	Limits               Limits     `json:"limits"`
	DedupeWindowMinutes  float64    `json:"dedupe_window_minutes"`
	// AllowedMediaDirs adds folders to downloads_dir and exports_dir, which are always allowed.
	AllowedMediaDirs []string `json:"allowed_media_dirs"`
	// DeniedPaths are never sent: the store folder and the config file.
	DeniedPaths    []string `json:"-"`
	ownMedia       []string
	foreignParents []string
}

type QuietHours struct {
	Enabled bool   `json:"enabled"`
	Start   string `json:"start"`
	End     string `json:"end"`
	Action  string `json:"action"` // queue | refuse
}

type Limits struct {
	PerChatPerHour                  int `json:"per_chat_per_hour"`
	PerHour                         int `json:"per_hour"`
	PerDay                          int `json:"per_day"`
	ChatsPerIdenticalMessagePerHour int `json:"chats_per_identical_message_per_hour"`
}

type Webhook struct {
	URL       string   `json:"url"`
	Events    []string `json:"events"`
	SecretEnv string   `json:"secret_env"`
}

// Encryption is defined in encryption.go.

// WebhookEvents lists the event names a webhook can subscribe to.
var WebhookEvents = []string{"message", "receipt", "call", "connection", "scheduled", "security", "agent.reply"}

// pythonSections are read by the Python package only; their content is not checked here.
var pythonSections = map[string]bool{"transcription": true, "people": true, "tasks": true, "mentions": true, "insight": true, "ai": true, "agent": true, "vision": true, "server": true}

// pythonKeys are single keys inside bridge sections that only the Python package
// reads; the bridge accepts and ignores them.
var pythonKeys = map[string]bool{"sending.allow_confirm_flag": true}

// Default returns the built-in configuration (sending off, everything local).
func Default() *Config {
	return &Config{
		StoreDir:     "store",
		DownloadsDir: "downloads",
		ExportsDir:   "exports",
		SessionFile:  DefaultSessionFile,
		API:          API{Listen: "127.0.0.1:8080"},
		Login:        Login{DeviceName: "WA MCP", DevicePlatform: "desktop", OpenWindow: true, Public: LoginPublic{Listen: "127.0.0.1:8766"}},
		ReadReceipts: ReadReceipts{AllowMarkRead: true},
		Calls:        Calls{Record: true},
		Media:        Media{MaxDownloadMB: 512},
		Sending: Sending{
			RequireConfirm:       true,
			HumanLike:            true,
			MarkReadBeforeReply:  true,
			TypingCharsPerSecond: 7,
			TypingMinSeconds:     1.2,
			TypingMaxSeconds:     20,
			Jitter:               0.3,
			PauseBetween:         []float64{1.0, 3.5},
			QuietHours:           QuietHours{Start: "23:00", End: "08:00", Action: "queue"},
			Limits:               Limits{PerChatPerHour: 20, PerHour: 60, PerDay: 300, ChatsPerIdenticalMessagePerHour: 3},
			DedupeWindowMinutes:  10,
			AllowedMediaDirs:     []string{},
		},
		Agent:      Agent{Mode: "draft"},
		Webhooks:   []Webhook{},
		Encryption: defaultEncryption(),
		Import:     defaultImport(),
		Accounts:   map[string]Account{},
	}
}

// ResolvePath picks the config file: flag value, then WA_MCP_CONFIG, then ./config.json.
func ResolvePath(flagValue string) string {
	if flagValue != "" {
		return flagValue
	}
	if v := os.Getenv("WA_MCP_CONFIG"); v != "" {
		return v
	}
	return "config.json"
}

// Load reads the file at path (a missing file means defaults), applies
// environment overrides, resolves relative paths and validates the result.
func Load(path string) (*Config, error) { return LoadAccount(path, "") }

// LoadAccount is Load for one entry of "accounts" ("" = "default").
func LoadAccount(path, account string) (*Config, error) {
	cfg := Default()
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	cfg.File = abs
	cfg.Dir = filepath.Dir(abs)

	// File-backed credentials consumed by the Python services must also stay
	// out of bridge sends and imports, including custom environment names.
	var secrets struct {
		AI struct {
			Env string `json:"api_key_env"`
		} `json:"ai"`
		Transcription struct {
			HTTP struct {
				Env string `json:"api_key_env"`
			} `json:"http"`
		} `json:"transcription"`
		Server struct {
			OAuth struct {
				Env string `json:"admin_password_env"`
			} `json:"oauth"`
		} `json:"server"`
	}
	secrets.AI.Env, secrets.Transcription.HTTP.Env, secrets.Server.OAuth.Env = "WA_MCP_AI_API_KEY", "WA_MCP_STT_API_KEY", "WA_MCP_ADMIN_PASSWORD"
	data, err := os.ReadFile(abs)
	switch {
	case err == nil:
		data = bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))
		if err := json.Unmarshal(data, cfg); err != nil {
			return nil, fmt.Errorf("config %s: %w", abs, err)
		}
		if err := json.Unmarshal(data, &secrets); err != nil {
			return nil, fmt.Errorf("config %s: invalid credential setting: %w", abs, err)
		}
		cfg.encryptionCompat(data, abs)
		for _, k := range UnknownKeys(data) {
			cfg.Warnings = append(cfg.Warnings, UnknownKeyWarning(abs, k))
		}
	case errors.Is(err, os.ErrNotExist):
	default:
		return nil, err
	}
	cfg.secretEnvNames = []string{secrets.AI.Env, secrets.Transcription.HTTP.Env, secrets.Server.OAuth.Env}

	if err := cfg.applyEnv(); err != nil {
		return nil, err
	}
	if err := cfg.selectAccount(account); err != nil {
		return nil, fmt.Errorf("config %s: %w", abs, err)
	}
	cfg.resolvePaths()
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("config %s: %w", abs, err)
	}
	return cfg, nil
}

// UnknownKeys lists keys in a config document that no bridge setting uses
// (Python-only sections are accepted as they are).
func UnknownKeys(data []byte) []string {
	var out []string
	walkKeys(data, reflect.TypeOf(Config{}), "", &out)
	sort.Strings(out)
	return out
}

func walkKeys(raw []byte, t reflect.Type, prefix string, out *[]string) {
	switch t.Kind() {
	case reflect.Struct:
		var m map[string]json.RawMessage
		if json.Unmarshal(raw, &m) != nil {
			return
		}
		fields := map[string]reflect.Type{}
		for i := 0; i < t.NumField(); i++ {
			name, _, _ := strings.Cut(t.Field(i).Tag.Get("json"), ",")
			if name != "" && name != "-" {
				fields[name] = t.Field(i).Type
			}
		}
		for k, v := range m {
			if prefix == "" && pythonSections[k] {
				continue
			}
			if ft, ok := fields[k]; ok {
				walkKeys(v, ft, prefix+k+".", out)
			} else if !(prefix == "" && pythonSections[k]) && !pythonKeys[stripAccount(prefix+k)] {
				*out = append(*out, prefix+k)
			}
		}
	case reflect.Map:
		var m map[string]json.RawMessage
		if json.Unmarshal(raw, &m) != nil {
			return
		}
		for k, v := range m {
			walkKeys(v, t.Elem(), prefix+k+".", out)
		}
	case reflect.Slice:
		if t.Elem().Kind() != reflect.Struct {
			return
		}
		var items []json.RawMessage
		if json.Unmarshal(raw, &items) != nil {
			return
		}
		base := strings.TrimSuffix(prefix, ".")
		for i, item := range items {
			walkKeys(item, t.Elem(), fmt.Sprintf("%s[%d].", base, i), out)
		}
	}
}

// EnvOverrides documents the supported environment variables.
var EnvOverrides = []string{
	"WA_MCP_STORE_DIR", "WA_MCP_DOWNLOADS_DIR", "WA_MCP_EXPORTS_DIR",
	"WA_MCP_API_LISTEN", "WA_MCP_API_TOKEN", "WA_MCP_SENDING_ENABLED", "WA_MCP_LOGIN_OPEN_WINDOW",
	"WA_MCP_LOGIN_PUBLIC_URL", "WA_MCP_ENCRYPTION_MODE", "WA_MCP_KEY_FILE",
}

func (c *Config) applyEnv() error {
	str := func(name string, dst *string) {
		if v, ok := os.LookupEnv(name); ok && v != "" {
			*dst = v
		}
	}
	boolean := func(name string, dst *bool) error {
		v, ok := os.LookupEnv(name)
		if !ok || v == "" {
			return nil
		}
		b, err := strconv.ParseBool(v)
		if err != nil {
			return fmt.Errorf("%s: want true or false, got %q", name, v)
		}
		*dst = b
		return nil
	}
	str("WA_MCP_STORE_DIR", &c.StoreDir)
	str("WA_MCP_DOWNLOADS_DIR", &c.DownloadsDir)
	str("WA_MCP_EXPORTS_DIR", &c.ExportsDir)
	str("WA_MCP_API_LISTEN", &c.API.Listen)
	str("WA_MCP_API_TOKEN", &c.API.Token)
	if err := boolean("WA_MCP_SENDING_ENABLED", &c.Sending.Enabled); err != nil {
		return err
	}
	str("WA_MCP_LOGIN_PUBLIC_URL", &c.Login.Public.URL)
	str("WA_MCP_ENCRYPTION_MODE", &c.Encryption.Mode)
	str("WA_MCP_KEY_FILE", &c.Encryption.KeyFile)
	return boolean("WA_MCP_LOGIN_OPEN_WINDOW", &c.Login.OpenWindow)
}

// ExpandHome replaces a leading "~" (alone or before a path separator) with the
// home folder, like Python's os.path.expanduser for the current user.
func ExpandHome(p string) string {
	if p != "~" && !strings.HasPrefix(p, "~/") && !strings.HasPrefix(p, "~\\") {
		return p
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return p
	}
	return filepath.Join(home, p[1:])
}

func (c *Config) abs(p string) string {
	p = ExpandHome(p)
	if p == "" || filepath.IsAbs(p) {
		return filepath.Clean(p)
	}
	return filepath.Join(c.Dir, p)
}

func (c *Config) resolvePaths() {
	c.StoreDir = c.abs(c.StoreDir)
	c.DownloadsDir = c.abs(c.DownloadsDir)
	c.ExportsDir = c.abs(c.ExportsDir)
	dirs := []string{c.DownloadsDir, c.ExportsDir}
	for _, d := range c.Sending.AllowedMediaDirs {
		if d = strings.TrimSpace(d); d != "" && !contains(dirs, c.abs(d)) {
			dirs = append(dirs, c.abs(d))
		}
	}
	c.Sending.AllowedMediaDirs = dirs
	c.Sending.DeniedPaths = []string{c.StoreDir, c.File}
	c.resolveEncryption()
	c.resolveImport()
}

// Validate checks value ranges and fills small gaps with defaults.
func (c *Config) Validate() error {
	if c.StoreDir == "" {
		return errors.New("store_dir is empty")
	}
	if err := c.validateSessionFile(); err != nil {
		return err
	}
	if _, _, err := net.SplitHostPort(c.API.Listen); err != nil {
		return fmt.Errorf("api.listen: %v", err)
	}
	if strings.TrimSpace(c.Login.DeviceName) == "" || strings.ContainsAny(c.Login.DeviceName, "\r\n") {
		return errors.New("login.device_name must be a non-empty single line")
	}
	switch c.Login.DevicePlatform {
	case "desktop", "chrome", "firefox", "safari", "edge", "unknown":
	default:
		return errors.New("login.device_platform must be desktop, chrome, firefox, safari, edge or unknown")
	}
	if c.Login.Port < 0 || c.Login.Port > 65535 {
		return errors.New("login.port: out of range")
	}
	if err := c.Login.Public.validate(); err != nil {
		return err
	}
	if c.Media.MaxDownloadMB <= 0 {
		return errors.New("media.max_download_mb must be > 0")
	}
	if err := c.validateImport(); err != nil {
		return err
	}

	s := &c.Sending
	if s.TypingCharsPerSecond <= 0 {
		return errors.New("sending.typing_chars_per_second must be > 0")
	}
	if s.TypingMinSeconds < 0 || s.TypingMaxSeconds < s.TypingMinSeconds {
		return errors.New("sending.typing_min_seconds/typing_max_seconds: need 0 <= min <= max")
	}
	if s.Jitter < 0 || s.Jitter >= 1 {
		return errors.New("sending.jitter must be in [0, 1)")
	}
	if len(s.PauseBetween) != 2 || s.PauseBetween[0] < 0 || s.PauseBetween[1] < s.PauseBetween[0] {
		return errors.New("sending.pause_between_messages_seconds: need [min, max] with 0 <= min <= max")
	}
	if s.Limits.PerChatPerHour < 0 || s.Limits.PerHour < 0 || s.Limits.PerDay < 0 || s.Limits.ChatsPerIdenticalMessagePerHour < 0 {
		return errors.New("sending.limits: values must be >= 0 (0 = no limit)")
	}
	if s.DedupeWindowMinutes < 0 {
		return errors.New("sending.dedupe_window_minutes must be >= 0")
	}
	q := &s.QuietHours
	if _, err := ParseClock(q.Start); err != nil {
		return fmt.Errorf("sending.quiet_hours.start: %v", err)
	}
	if _, err := ParseClock(q.End); err != nil {
		return fmt.Errorf("sending.quiet_hours.end: %v", err)
	}
	switch q.Action {
	case "queue", "refuse":
	case "":
		q.Action = "queue"
	default:
		return fmt.Errorf("sending.quiet_hours.action: want queue or refuse, got %q", q.Action)
	}

	for i, w := range c.Webhooks {
		u, err := url.Parse(w.URL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return fmt.Errorf("webhooks[%d].url: need an http(s) URL", i)
		}
		for _, e := range w.Events {
			if !knownEvent(e) {
				return fmt.Errorf("webhooks[%d].events: unknown event %q", i, e)
			}
		}
	}
	if err := c.Encryption.validate(); err != nil {
		return err
	}
	return nil
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

func knownEvent(e string) bool {
	for _, k := range WebhookEvents {
		if k == e {
			return true
		}
	}
	return false
}

// ParseClock parses "HH:MM" into an offset from midnight.
func ParseClock(s string) (time.Duration, error) {
	t, err := time.Parse("15:04", strings.TrimSpace(s))
	if err != nil {
		return 0, fmt.Errorf("want HH:MM, got %q", s)
	}
	return time.Duration(t.Hour())*time.Hour + time.Duration(t.Minute())*time.Minute, nil
}

// MaxDownloadBytes is the attachment size limit in bytes.
func (c *Config) MaxDownloadBytes() int64 { return int64(c.Media.MaxDownloadMB) << 20 }

// SessionDB is the whatsmeow session database path.
func (c *Config) SessionDB() string {
	if filepath.IsAbs(c.SessionFile) {
		return c.SessionFile
	}
	return filepath.Join(c.StoreDir, c.SessionFile)
}

// MessagesDB is the bridge message database path.
func (c *Config) MessagesDB() string { return filepath.Join(c.StoreDir, "wa.db") }

// TokenFile is where the generated API token is kept.
func (c *Config) TokenFile() string { return filepath.Join(c.StoreDir, "api.token") }

// LockFile is held by a running bridge so a second one (or an import) cannot use the store.
func (c *Config) LockFile() string { return filepath.Join(c.StoreDir, "bridge.lock") }
