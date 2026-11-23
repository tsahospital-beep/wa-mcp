package wabridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/accounts"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/login"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/schedule"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/webhook"
)

func (o Options) stdout() io.Writer {
	if o.Stdout != nil {
		return o.Stdout
	}
	return os.Stdout
}

func (o Options) stderr() io.Writer {
	if o.Stderr != nil {
		return o.Stderr
	}
	return os.Stderr
}

func (o Options) version() string {
	if o.Version != "" {
		return o.Version
	}
	return "dev"
}

// Run starts the bridge with opts and serves until ctx ends or the bridge
// stops with an error.
func Run(ctx context.Context, opts Options) error {
	logger := log.New(opts.stdout(), "", log.LstdFlags)
	cfg, err := loadConfig(opts, "")
	if err != nil {
		return fatal(err)
	}
	for _, w := range cfg.Warnings {
		logger.Printf("warning: %s", w)
	}
	return serve(ctx, cfg, logger, opts)
}

// loadConfig reads the config of one account with the embedder settings of opts applied.
func loadConfig(opts Options, account string) (*config.Config, error) {
	cfg, err := config.LoadAccount(config.ResolvePath(opts.ConfigPath), account)
	if err != nil {
		return nil, err
	}
	if opts.SessionFile != "" {
		if err := cfg.SetSessionFile(opts.SessionFile); err != nil {
			return nil, err
		}
	}
	if err := decodeSections(cfg, opts.ConfigSections); err != nil {
		return nil, err
	}
	return cfg, nil
}

// decodeSections fills the embedder sections and drops their unknown-key warnings.
func decodeSections(cfg *config.Config, sections map[string]any) error {
	if len(sections) == 0 {
		return nil
	}
	data, err := os.ReadFile(cfg.File)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	data = bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))
	var top map[string]json.RawMessage
	if err := json.Unmarshal(data, &top); err != nil {
		return fmt.Errorf("config %s: %w", cfg.File, err)
	}
	for name, dst := range sections {
		raw, ok := top[name]
		if !ok {
			continue
		}
		if err := json.Unmarshal(raw, dst); err != nil {
			return fmt.Errorf("config %s: %s: %w", cfg.File, name, err)
		}
	}
	ours := map[string]bool{}
	for _, k := range config.UnknownKeys(data) {
		name, _, _ := strings.Cut(k, ".")
		name, _, _ = strings.Cut(name, "[")
		if _, ok := sections[name]; ok {
			ours[config.UnknownKeyWarning(cfg.File, k)] = true
		}
	}
	kept := cfg.Warnings[:0]
	for _, w := range cfg.Warnings {
		if !ours[w] {
			kept = append(kept, w)
		}
	}
	cfg.Warnings = kept
	return nil
}

// serve runs one bridge until ctx ends; errors are returned, notes go to logger.
func serve(ctx context.Context, cfg *config.Config, logger *log.Logger, opts Options) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	version := opts.version()
	logger.Printf("wa-bridge %s starting", version)
	if _, err := os.Stat(cfg.File); err != nil {
		logger.Printf("config: %s not found, using defaults", cfg.File)
	} else {
		logger.Printf("config: %s", cfg.File)
	}
	release, err := lockStore(cfg)
	if err != nil {
		return err
	}
	defer release()
	// The store belongs to the bridge; downloads_dir may be a folder of the user.
	if err := fsutil.MkdirOwned(cfg.StoreDir); err != nil {
		return err
	}
	if err := fsutil.MkdirPrivate(cfg.DownloadsDir); err != nil {
		return err
	}
	if err := fsutil.RestrictTree(cfg.StoreDir); err != nil {
		logger.Printf("warning: could not restrict permissions on %s: %v", cfg.StoreDir, err)
	}
	if !fsutil.InsideHome(cfg.StoreDir) {
		logger.Printf("warning: store_dir %s is outside your home folder; make sure other users cannot read it", cfg.StoreDir)
	}
	efs := protectFolder(cfg, logger)
	// Partial downloads and import uploads left by a crash hold plain media and chat text.
	// Folders of the other accounts are skipped: their bridges may be downloading right now.
	others := cfg.OtherAccountFolders()
	swept := fsutil.SweepTemp(filepath.Join(cfg.StoreDir, "import-tmp"), others, "upload-*") +
		fsutil.SweepTemp(cfg.DownloadsDir, others, ".part-*")
	if swept > 0 {
		logger.Printf("store: removed %d leftover temporary files", swept)
	}

	libLevel := strings.ToUpper(strings.TrimSpace(os.Getenv("WA_MCP_LOG_LEVEL")))
	var debug func(string, ...any)
	if libLevel == "DEBUG" {
		debug = func(format string, args ...any) { logger.Printf("debug: "+format, args...) }
	}
	db, err := openStore(ctx, cfg, debug)
	if err != nil {
		return err // openStore marks key and schema errors as fatal
	}
	defer db.Close()
	if o := observer(opts, logger); o != nil {
		db.SetObserver(o)
	}
	schema, _ := db.SchemaVersion(ctx)
	logKeySetup(logger, cfg, db, schema)

	token, err := api.LoadToken(cfg)
	if err != nil {
		return fmt.Errorf("api token: %w", err)
	}
	logger.Printf("api token: %s", api.TokenSource(cfg))

	var self atomic.Value // func() string, set once the WhatsApp client exists
	hooks, notes := webhook.New(cfg.Webhooks, os.Getenv, webhook.Options{Log: logger, UserAgent: "wa-mcp-bridge/" + version,
		Account: cfg.Account, Self: func() string {
			if f, ok := self.Load().(func() string); ok {
				return f()
			}
			return ""
		}})
	for _, n := range notes {
		logger.Printf("warning: %s", n)
	}
	hooks.Start(ctx)
	if hooks.Enabled() {
		logger.Printf("webhooks: %d configured", len(cfg.Webhooks))
	}
	mon := &security.Monitor{Store: db, Log: logger, Emit: hooks.Emit}
	mon.SetProtection(protection(cfg, db, efs))

	if err := prepareSessionDir(cfg, logger); err != nil { // protect.go
		return err
	}
	var feed api.AgentFeed
	if cfg.Agent.Enabled {
		feed = make(api.AgentFeed, 256)
	}
	client, err := wa.New(ctx, db, wa.Options{
		SessionPath:    cfg.SessionDB(),
		DeviceName:     cfg.Login.DeviceName,
		DevicePlatform: cfg.Login.DevicePlatform,
		Log:            logger,
		LibLogLevel:    libLevel,
		RecordCalls:    cfg.Calls.Record,
		AutoMarkRead:   cfg.ReadReceipts.AutoMarkRead && cfg.ReadReceipts.AllowMarkRead,
		Emit: func(event string, data any) {
			hooks.Emit(event, data)
			if (event == "message" || event == "call") && !feed.Offer(data) {
				logger.Print("agent skipped event: input queue full")
			}
		},
		Enter:         enterPresses(),
		TerminalIsTTY: isTerminal(os.Stdout),
		Security:      mon.Record,
		NewLoginUI: func() (wa.LoginUI, error) {
			w := login.New(accounts.WithAccounts(cfg, loginOptions(cfg, nil)))
			if err := w.Start(); err != nil {
				return nil, err
			}
			return w, nil
		},
	})
	if err != nil {
		return err
	}
	self.Store(client.Account)
	// Close waits for running event handlers, so the database is closed after them.
	closeClient := sync.OnceFunc(client.Close)
	defer closeClient()
	// wa.New creates session.db and its WAL files on the first start; limit them to the user right away.
	if err := fsutil.RestrictTree(cfg.StoreDir); err != nil {
		logger.Printf("warning: could not restrict permissions on %s: %v", cfg.StoreDir, err)
	}
	restrictSession(cfg, logger) // protect.go

	snd := &sender.Sender{BuildAction: client.BuildAction, AgentCurrent: cfg.AgentCurrent, Cfg: cfg.Sending, DB: db, Transport: client, Log: logger, NoMarkRead: !cfg.ReadReceipts.AllowMarkRead}
	fetcher := &media.Fetcher{DB: db, Src: client, Dir: cfg.DownloadsDir, MaxBytes: cfg.MaxDownloadBytes(), Private: db.Encrypted()}
	runner := &schedule.Runner{
		DB: db, Send: snd.Send, Emit: hooks.Emit, Log: logger,
		Enabled: func() bool { return cfg.Sending.Enabled && client.Connected() },
	}
	srv := &api.Server{
		Agent: feed, Emit: hooks.Emit, RejectCall: client.RejectCall,
		Cfg: cfg, DB: db, WA: client, Send: snd.Send, Media: fetcher, Token: token, Version: version, Log: logger, Security: mon, WebhookDropped: hooks.Dropped,
		Avatar: func(ctx context.Context, jid types.JID) (string, error) {
			return media.SaveAvatar(ctx, client, nil, cfg.DownloadsDir, jid)
		},
	}
	d := &deps{cfg: cfg, db: db, client: client, send: snd.Send, fetch: fetcher}
	if srv.Extra, err = collectRoutes(opts.Routes, d); err != nil {
		return fmt.Errorf("routes: %w", err)
	}
	if err := srv.CheckExtra(); err != nil {
		return err
	}
	if len(srv.Extra) > 0 {
		logger.Printf("api: %d extra routes", len(srv.Extra))
	}

	if w := listenWarning(cfg.API.Listen); w != "" {
		logger.Printf("warning: %s", w)
	}
	apiDone := make(chan error, 1)
	go func() {
		apiDone <- srv.Serve(ctx, func(addr string) { logger.Printf("api: listening on http://%s/api/v1", addr) })
	}()
	if cfg.Sending.Enabled {
		logger.Printf("sending: enabled (limits %d/chat/hour, %d/hour, %d/day; human-like pacing %v)",
			cfg.Sending.Limits.PerChatPerHour, cfg.Sending.Limits.PerHour, cfg.Sending.Limits.PerDay, cfg.Sending.HumanLike)
	} else {
		logger.Printf("sending: disabled (set sending.enabled=true to allow it)")
	}

	startDone := make(chan error, 1)
	go func() { startDone <- client.Start(ctx) }()
	schedDone := make(chan struct{})
	go func() { runner.Run(ctx); close(schedDone) }()

	var runErr error
	for running := true; running; {
		select {
		case err := <-startDone:
			if err != nil && ctx.Err() == nil {
				runErr = err
				if errors.Is(err, wa.ErrAccountMismatch) {
					runErr = fatal(err)
				}
				running = false
			}
		case err := <-apiDone:
			runErr = err
			apiDone = nil
			running = false
		case <-ctx.Done():
			running = false
		}
	}

	logger.Printf("shutting down")
	cancel()
	if apiDone != nil {
		<-apiDone
	}
	<-schedDone
	closeClient()
	common.WaitTimeout(hooks.Wait, 3*time.Second)
	logger.Printf("stopped")
	return runErr
}
