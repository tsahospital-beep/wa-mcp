package wabridge

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/keystore"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// errCopiedStore marks a store whose keystore key is missing here.
var errCopiedStore = errors.New("no key for this store")

// errSecretFile marks a key file or passphrase file that cannot be used
// (missing, too large, readable by others); a restart does not help.
var errSecretFile = errors.New("secret file")

// keyPlan turns the encryption settings into the key plan for wa.db.
// In mode keystore without a usable keystore, a set passphrase or a
// configured key file is used for a new store instead.
func keyPlan(cfg *config.Config, ks keystore.Backend, getenv func(string) string) store.KeyPlan {
	e := cfg.Encryption
	// The passphrase comes from the variable, or from the file named in <variable>_FILE (Docker secrets).
	pass, passErr := "", error(nil)
	if e.PassphraseEnv != "" {
		pass = getenv(e.PassphraseEnv)
		if file := getenv(e.PassphraseEnv + "_FILE"); pass == "" && file != "" {
			pass, passErr = keystore.ReadPassphraseFile(file)
		}
	}
	u := store.KeyPlan{}
	switch e.Mode {
	case config.ModeOff:
	case config.ModePassphrase:
		u.NewKind = store.KindPassphrase
	case config.ModeKeyfile:
		u.NewKind = store.KindKeyfile
	default:
		u.NewKind = store.KindKeystore
		if keystore.Available(ks) != nil {
			switch {
			case pass != "" || passErr != nil:
				u.NewKind = store.KindPassphrase
			case e.KeyFile != "":
				u.NewKind = store.KindKeyfile
			}
		}
	}
	db := cfg.MessagesDB()
	u.Secret = func(kind string, create bool) ([]byte, error) {
		switch kind {
		case store.KindPassphrase:
			if passErr != nil {
				return nil, fmt.Errorf("%w: %s_FILE: %w", errSecretFile, e.PassphraseEnv, passErr)
			}
			if pass != "" {
				return []byte(pass), nil
			}
			if create {
				return nil, fmt.Errorf("%w: encryption.mode is passphrase but %s is empty; set the passphrase in that environment variable",
					store.ErrNoPassphrase, e.PassphraseEnv)
			}
			return nil, fmt.Errorf("%w: %s is encrypted with a passphrase but %s is empty; set the passphrase in that environment variable",
				store.ErrNoPassphrase, db, e.PassphraseEnv)
		case store.KindKeyfile:
			if e.KeyFile == "" {
				return nil, fmt.Errorf("%w: %s is encrypted with a key file: set encryption.key_file or WA_MCP_KEY_FILE", errSecretFile, db)
			}
			k, err := keystore.ReadKeyFile(e.KeyFile)
			if err != nil {
				return nil, fmt.Errorf("%w: %w", errSecretFile, err)
			}
			return k, nil
		case store.KindKeystore:
			k, err := keystore.StoreKey(ks, create)
			switch {
			case err == nil:
				return k, nil
			case errors.Is(err, keystore.ErrNotFound):
				return nil, fmt.Errorf("%w: %s is encrypted with a key from the OS keystore, but %s holds no key for this store. "+
					"The store was copied from another computer or Windows user, or the key was removed; "+
					"it cannot be opened here", errCopiedStore, db, ks.Describe(keystore.ItemStoreKey))
			case errors.Is(err, keystore.ErrUnavailable) && create:
				return nil, fmt.Errorf("%w. Install it, or set encryption.mode to passphrase (with %s set) or keyfile "+
					"(with encryption.key_file), or to off to keep %s unencrypted", err, e.PassphraseEnv, db)
			}
			return nil, err
		}
		return nil, fmt.Errorf("unknown key kind %q", kind)
	}
	return u
}

// setupStoreKey applies the encryption settings to an open wa.db.
func setupStoreKey(ctx context.Context, cfg *config.Config, db *store.DB) error {
	st, err := db.SetupKey(ctx, keyPlan(cfg, keystore.ForStore(cfg.StoreDir), os.Getenv))
	switch {
	case err == nil:
		return nil
	case errors.Is(err, store.ErrWrongPassphrase):
		return fatal(fmt.Errorf("the passphrase in %s does not open %s", cfg.Encryption.PassphraseEnv, cfg.MessagesDB()))
	case errors.Is(err, store.ErrWrongKey):
		return fatal(fmt.Errorf("the %s key does not open %s", st.Kind, cfg.MessagesDB()))
	case errors.Is(err, store.ErrEncryptedStore):
		return fatal(fmt.Errorf("%s is encrypted, but encryption.mode is off; set encryption.mode to the mode it was encrypted with (default keystore)",
			cfg.MessagesDB()))
	case errors.Is(err, store.ErrNoPassphrase), errors.Is(err, keystore.ErrNotFound), errors.Is(err, errCopiedStore),
		errors.Is(err, errSecretFile):
		return fatal(err)
	}
	// A keystore that does not answer may come back; such errors are not fatal.
	return err
}

// keyStoreName names where the key of kind lives, for the log and the status.
func keyStoreName(cfg *config.Config, kind string) string {
	switch kind {
	case store.KindKeystore:
		return keystore.ForStore(cfg.StoreDir).Name()
	case store.KindKeyfile:
		return "file"
	case store.KindPassphrase:
		return "env"
	}
	return "none"
}

// logKeySetup reports the encryption state of wa.db (never a key or a passphrase).
func logKeySetup(logger *log.Logger, cfg *config.Config, db *store.DB, schema int) {
	st := db.KeySetup()
	enc := "off"
	switch st.Kind {
	case store.KindKeystore:
		enc = "on, key in " + keystore.ForStore(cfg.StoreDir).Describe(keystore.ItemStoreKey)
	case store.KindKeyfile:
		enc = "on, key file " + cfg.Encryption.KeyFile
	case store.KindPassphrase:
		enc = "on, passphrase from " + cfg.Encryption.PassphraseEnv
	}
	logger.Printf("database: %s (schema %d, encryption %s)", db.Path(), schema, enc)
	if st.Migrated > 0 {
		logger.Printf("database: encrypted %d existing fields in place; the plain copy made before was wiped", st.Migrated)
	}
	if st.KeptBackup != "" {
		logger.Printf("warning: database: could not remove the plain copy %s made before encryption; delete it yourself", st.KeptBackup)
	}
	if st.CompactPending {
		logger.Printf("warning: database: another program kept the database busy, so old plain text may still be in free pages " +
			"or the WAL; the next start compacts the file again (stop the MCP server before it to be sure)")
	}
	if st.Kind == "" {
		logger.Printf("warning: database: field encryption is off (encryption.mode=off)")
	}
}

// protectFolder applies Windows EFS to the store folder when enabled and logs the state.
func protectFolder(cfg *config.Config, logger *log.Logger) fsutil.EFSResult {
	if !cfg.Encryption.EFS {
		return fsutil.EFSResult{State: "disabled"}
	}
	res := fsutil.EncryptEFS(cfg.StoreDir, "bridge.lock")
	switch res.State {
	case fsutil.EFSOn:
		logger.Printf("store: Windows EFS is on for %s", cfg.StoreDir)
	case fsutil.EFSPartial:
		logger.Printf("warning: store: Windows EFS is on for %s, but these files stayed unencrypted (they are in use; "+
			"the next start tries again): %s", cfg.StoreDir, strings.Join(res.Failed, ", "))
	case fsutil.EFSUnavailable:
		logger.Printf("store: Windows EFS is not available for %s (%s); use BitLocker or another disk encryption", cfg.StoreDir, res.Reason)
	}
	return res
}

// protection is the summary shown in /api/v1/status (no secrets).
func protection(cfg *config.Config, db *store.DB, efs fsutil.EFSResult) map[string]string {
	kind := db.KeySetup().Kind
	field := kind
	if field == "" {
		field = config.ModeOff
	}
	token := "file"
	switch src := api.TokenSource(cfg); {
	case src == "from config":
		token = "config"
	case src != cfg.TokenFile():
		token = keystore.ForStore(cfg.StoreDir).Name()
	}
	st := db.KeySetup()
	compaction, copyLeft := "done", "none"
	if st.CompactPending {
		compaction = "pending"
	}
	if st.KeptBackup != "" {
		copyLeft = "kept"
	}
	return map[string]string{
		"field_encryption": field,
		"key_store":        keyStoreName(cfg, kind),
		"api_token":        token,
		"efs":              efs.State,
		"compaction":       compaction,
		"plain_copy":       copyLeft,
	}
}

// apiTokenCmd prints the API token for scripts (it may live in the OS keystore).
func apiTokenCmd(args []string, stdout, stderr io.Writer, defaultPath string) int {
	fs := flag.NewFlagSet("api-token", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", defaultPath, "path of config.json")
	account := fs.String("account", "", `account from "accounts" in the config`)
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
	tok, err := api.ReadToken(cfg)
	if err == nil && tok == "" {
		err = errors.New("no API token yet: start the bridge once")
	}
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	fmt.Fprintln(stdout, tok)
	return 0
}

// sessionOutsideStore reports whether session_file points out of store_dir
// (an absolute path, for example a volume that only the bridge mounts).
func sessionOutsideStore(cfg *config.Config) bool {
	return !fsutil.Inside(cfg.StoreDir, cfg.SessionDB())
}

// prepareSessionDir creates the folder of an outside session file for the
// user only (0700, or the protected access list on Windows) and turns on EFS
// for it like for the store folder.
func prepareSessionDir(cfg *config.Config, logger *log.Logger) error {
	if !sessionOutsideStore(cfg) {
		return nil
	}
	dir := filepath.Dir(cfg.SessionDB())
	if err := fsutil.MkdirPrivate(dir); err != nil {
		return fmt.Errorf("session folder %s: %w", dir, err)
	}
	if cfg.Encryption.EFS {
		if res := fsutil.EncryptEFS(dir); res.State == fsutil.EFSPartial {
			logger.Printf("warning: session folder: Windows EFS left these files unencrypted: %s", strings.Join(res.Failed, ", "))
		}
	}
	return nil
}

// restrictSession limits the session file and its WAL files to the user (0600 or the access list).
func restrictSession(cfg *config.Config, logger *log.Logger) {
	if !sessionOutsideStore(cfg) {
		return
	}
	for _, p := range []string{cfg.SessionDB(), cfg.SessionDB() + "-wal", cfg.SessionDB() + "-shm"} {
		if _, err := os.Stat(p); err != nil {
			continue
		}
		if err := fsutil.Restrict(p); err != nil {
			logger.Printf("warning: could not restrict permissions on %s: %v", p, err)
		}
	}
}
