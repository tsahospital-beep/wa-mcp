// The wa-bridge command line (moved from cmd/wa-bridge, which now only calls Main).

package wabridge

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/accounts"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/login"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

const usageText = `wa-bridge connects one WhatsApp account (as a linked device) and serves
a local HTTP API for the wa-mcp tools.

Usage:
  wa-bridge [--config FILE] [--account NAME]
      Run the bridge. On first start (or after a logout) a login window shows
      a QR code; the same code is printed here as a fallback. --account picks
      an entry of "accounts" in the config (default: the account "default").
  wa-bridge --login-preview [--preview-scale N] [--config FILE] [--account NAME]
      Show the login window with fake codes. Never connects to WhatsApp.
  wa-bridge login-url [--config FILE] [--account NAME]
      Print a fresh one-time link to the login window of the running bridge
      (for when its output goes to a log file instead of a terminal).
  wa-bridge accounts [--config FILE]
      List the configured accounts and whether each one runs and is linked.
  wa-bridge run-all [--config FILE]
      Run one bridge per account and start a bridge again when it stops.
  wa-bridge health [--config FILE]
      Exit 0 when the bridge answers GET /health (for container health checks).
  wa-bridge api-token [--config FILE] [--account NAME]
      Print the API token (it may be kept in the OS keystore) for scripts.
  wa-bridge import-legacy --from FILE [--session FILE] [--config FILE]
      Copy chats and messages from an older bridge database (messages.db).
      Stop the bridge (and any older bridge using that file) first.
  wa-bridge import-export --file FILE --chat CHAT [--me NAME] [--date-order dmy|mdy|ymd] [--dry-run]
      Store the messages of a WhatsApp "Export chat" file (.txt or .zip) in one
      chat, for history older than the phone sync. Stop the bridge first.
  wa-bridge --version

Flags:
`

const envText = `
Environment:
  WA_MCP_CONFIG            config file when --config is not given (default ./config.json)
  WA_MCP_STORE_DIR, WA_MCP_DOWNLOADS_DIR, WA_MCP_EXPORTS_DIR, WA_MCP_API_LISTEN,
  WA_MCP_API_TOKEN, WA_MCP_SENDING_ENABLED, WA_MCP_LOGIN_OPEN_WINDOW,
  WA_MCP_LOGIN_PUBLIC_URL  override the matching config values
  WA_MCP_ENCRYPTION_MODE   overrides encryption.mode: keystore (default), passphrase,
                           keyfile or off
  WA_MCP_KEY_FILE          overrides encryption.key_file (mode keyfile)
  WA_MCP_PASSPHRASE        database passphrase for encryption.mode passphrase
                           (the variable name is set by encryption.passphrase_env)
  WA_MCP_LOG_LEVEL         whatsmeow log level: ERROR, WARN (default), INFO, DEBUG

Safe defaults: sending stays off until sending.enabled=true, the API listens on
127.0.0.1 only and every request needs the token from store/api.token. Only
files in downloads_dir and exports_dir can be sent unless
sending.allowed_media_dirs says otherwise; the store folder never can.
`

// Main runs the wa-bridge command line with args (without the program name)
// and returns the exit code. The hooks and routes in opts apply when the
// bridge runs; --config replaces opts.ConfigPath.
func Main(args []string, opts Options) int {
	stdout, stderr := opts.stdout(), opts.stderr()
	if len(args) > 0 && args[0] == "import-legacy" {
		return importLegacy(args[1:], stdout, stderr, opts)
	}
	if len(args) > 0 && args[0] == "import-export" {
		return importExport(args[1:], stdout, stderr)
	}
	if len(args) > 0 && args[0] == "login-url" {
		return loginURL(args[1:], stdout, stderr)
	}
	if len(args) > 0 && args[0] == "health" {
		return health(args[1:], stdout, stderr)
	}
	if len(args) > 0 && args[0] == "api-token" {
		return apiTokenCmd(args[1:], stdout, stderr)
	}
	if len(args) > 0 && args[0] == "accounts" {
		return listAccounts(args[1:], stdout, stderr)
	}
	if len(args) > 0 && args[0] == "run-all" {
		return runAll(args[1:], stdout, stderr)
	}
	fs := flag.NewFlagSet("wa-bridge", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", "", "path of config.json (relative paths inside it are relative to its folder)")
	account := fs.String("account", "", `account from "accounts" in the config (default "default")`)
	preview := fs.Bool("login-preview", false, "show the login window with fake codes; no WhatsApp connection")
	scale := fs.Float64("preview-scale", 1, "with --login-preview: multiply code lifetimes (0.1 = ten times faster)")
	showVersion := fs.Bool("version", false, "print the version and exit")
	fs.Usage = func() {
		fmt.Fprint(stderr, usageText)
		fs.PrintDefaults()
		fmt.Fprint(stderr, envText)
	}
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "unknown argument %q (see --help)\n", fs.Arg(0))
		return 2
	}
	if *showVersion {
		fmt.Fprintln(stdout, "wa-bridge "+opts.version())
		return 0
	}
	logger := log.New(stdout, "", log.LstdFlags)
	if *configPath != "" {
		opts.ConfigPath = *configPath
	}
	cfg, err := loadConfig(opts, *account)
	if err != nil {
		logger.Printf("error: %v", err)
		return ExitFatal
	}
	for _, w := range cfg.Warnings {
		logger.Printf("warning: %s", w)
	}
	if len(cfg.AccountNames()) > 1 {
		logger.Printf("account: %s", cfg.Account)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *preview {
		go servePreviewHealth(ctx, cfg, logger)
		err := login.RunPreview(ctx, accounts.WithAccounts(cfg, loginOptions(cfg, stdout)), *scale)
		if err != nil {
			logger.Printf("error: %v", err)
			return 1
		}
		return 0
	}
	if err := serve(ctx, cfg, logger, opts); err != nil {
		logger.Printf("error: %v", err)
		if IsFatal(err) {
			return ExitFatal
		}
		return 1
	}
	return 0
}

// openStore opens wa.db and applies the encryption settings, with messages a user can act on.
func openStore(ctx context.Context, cfg *config.Config, debug func(string, ...any)) (*store.DB, error) {
	db, err := store.Open(cfg.MessagesDB())
	if err != nil {
		err = fmt.Errorf("database %s: %w", cfg.MessagesDB(), err)
		if errors.Is(err, store.ErrNewerSchema) {
			err = fatal(err)
		}
		return nil, err
	}
	db.Debug = debug
	if err := setupStoreKey(ctx, cfg, db); err != nil { // protect.go
		db.Close()
		return nil, err
	}
	return db, nil
}

// lockStore keeps a second bridge (or an import) away from the store while this one runs.
func lockStore(cfg *config.Config) (func(), error) {
	if err := fsutil.MkdirPrivate(cfg.StoreDir); err != nil {
		return nil, err
	}
	release, err := fsutil.Lock(cfg.LockFile())
	if errors.Is(err, fsutil.ErrLocked) {
		return nil, fmt.Errorf("another bridge is using %s; stop it first", cfg.StoreDir)
	}
	return release, err
}

// listenWarning describes who can reach a non-loopback listen address ("" for loopback).
func listenWarning(listen string) string {
	host, port, err := net.SplitHostPort(listen)
	if err != nil || common.IsLoopbackHost(host) {
		return ""
	}
	if common.IsUnspecifiedHost(host) {
		return fmt.Sprintf("api.listen %s accepts connections on every network interface; requests still need the token "+
			"and a Host header of localhost:%s or 127.0.0.1:%s (for example through a container port mapping)", listen, port, port)
	}
	return fmt.Sprintf("api.listen %s is not a loopback address: other machines that can reach it may call the API "+
		"with the token (Host header %s)", listen, listen)
}

// serve is in run.go.

// enterPresses signals each line typed in the terminal (used to ask for a new QR code).
func enterPresses() <-chan struct{} {
	ch := make(chan struct{}, 1)
	go func() {
		r := bufio.NewReader(os.Stdin)
		for {
			if _, err := r.ReadString('\n'); err != nil {
				return
			}
			select {
			case ch <- struct{}{}:
			default:
			}
		}
	}()
	return ch
}

func importLegacy(args []string, stdout, stderr io.Writer, opts Options) int {
	fs := flag.NewFlagSet("import-legacy", flag.ContinueOnError)
	fs.SetOutput(stderr)
	from := fs.String("from", "", "path of the older bridge database (for example store/messages.db)")
	session := fs.String("session", "", "older session database for LID authors (default: whatsapp.db next to --from, if present)")
	configPath := fs.String("config", "", "path of config.json")
	fs.Usage = func() {
		fmt.Fprint(stderr, "Usage: wa-bridge import-legacy --from FILE [--session FILE] [--config FILE]\n\n"+
			"Copies chats and messages from an older bridge database into store/wa.db.\n"+
			"Existing rows are kept; running it twice adds nothing. Stop the bridge first,\n"+
			"and stop any older bridge that still writes to the file given in --from.\n\nFlags:\n")
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if *from == "" {
		fs.Usage()
		return 2
	}
	if *configPath != "" {
		opts.ConfigPath = *configPath
	}
	cfg, err := loadConfig(opts, "")
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	release, err := lockStore(cfg)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	defer release()
	ctx := context.Background()
	db, err := openStore(ctx, cfg, nil)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	defer db.Close()
	lo := store.LegacyOptions{SessionDB: *session}
	if lo.SessionDB == "" {
		if sibling := filepath.Join(filepath.Dir(*from), "whatsapp.db"); fileExists(sibling) {
			lo.SessionDB = sibling
		}
	}
	st, err := db.ImportLegacy(ctx, *from, lo)
	if err != nil {
		fmt.Fprintf(stderr, "error: import: %v\n", err)
		return 1
	}
	fmt.Fprintf(stdout, "imported %d messages into %d chats (%d rows skipped) into %s\n",
		st.Messages, st.Conversations, st.Skipped, db.Path())
	return 0
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

// isTerminal reports whether f is an interactive console rather than a file or pipe.
func isTerminal(f *os.File) bool {
	st, err := f.Stat()
	return err == nil && st.Mode()&os.ModeCharDevice != 0
}

// loginURL asks the running bridge for a fresh one-time login link.
func loginURL(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("login-url", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", "", "path of config.json")
	account := fs.String("account", "", `account from "accounts" in the config (default "default")`)
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	cfg, err := config.LoadAccount(config.ResolvePath(*configPath), *account)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	if _, err := api.LoadToken(cfg); err != nil {
		fmt.Fprintf(stderr, "error: api token: %v\n", err)
		return 1
	}
	link, err := accounts.LoginLink(context.Background(), cfg)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	fmt.Fprintln(stdout, link)
	return 0
}
