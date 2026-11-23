package wabridge

import (
	"bytes"
	"context"
	"encoding/hex"
	"io"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/keystore"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/webhook"
)

// Tests in this package never touch the real OS keystore for the store key:
// a keychain entry per test run would pile up on a developer's Mac.
var testKeystore = keystore.NewMemory()

func init() { storeKeystore = func(string) keystore.Backend { return testKeystore } }

func TestKeyPlanModes(t *testing.T) {
	cfg := config.Default()
	cfg.StoreDir = t.TempDir()
	env := func(v string) func(string) string { return func(string) string { return v } }
	up := keystore.NewMemory()
	down := keystore.NewMemory()
	down.Err = keystore.ErrUnavailable

	if u := keyPlan(cfg, up, env("")); u.NewKind != store.KindKeystore {
		t.Fatalf("keystore: %q", u.NewKind)
	}
	if u := keyPlan(cfg, down, env("pass")); u.NewKind != store.KindPassphrase {
		t.Fatalf("fallback to the passphrase: %q", u.NewKind)
	}
	withFile := *cfg
	withFile.Encryption.KeyFile = filepath.Join(t.TempDir(), "k")
	if u := keyPlan(&withFile, down, env("")); u.NewKind != store.KindKeyfile {
		t.Fatalf("fallback to the key file: %q", u.NewKind)
	}
	u := keyPlan(cfg, down, env(""))
	if _, err := u.Secret(u.NewKind, true); err == nil || !strings.Contains(err.Error(), "encryption.mode") {
		t.Fatalf("no keystore and no fallback: %v", err)
	}
	if _, err := keyPlan(cfg, up, env("")).Secret(store.KindKeystore, false); err == nil || !strings.Contains(err.Error(), "copied") {
		t.Fatalf("missing key of an encrypted store: %v", err)
	}
	off := *cfg
	off.Encryption.Mode = config.ModeOff
	if u := keyPlan(&off, up, env("")); u.NewKind != "" {
		t.Fatalf("off: %q", u.NewKind)
	}
}

// TestStartLogsHoldNoSecrets runs the start-up steps that touch secrets and
// message text with one logger and checks that none of them reaches the log.
func TestStartLogsHoldNoSecrets(t *testing.T) {
	const text = "Alice says the gate code is 4711"
	const passphrase = "correct horse battery staple"
	const webhookSecret = "hook-secret-0123456789"
	var buf bytes.Buffer
	logger := log.New(&buf, "", 0)
	ctx := context.Background()

	for _, mode := range []string{config.ModeKeystore, config.ModePassphrase, config.ModeKeyfile} {
		dir := t.TempDir()
		keyFile := filepath.Join(dir, "store.key")
		key := bytes.Repeat([]byte{0x5a}, 32)
		if err := fsutil.WriteFile(keyFile, []byte(hex.EncodeToString(key))); err != nil {
			t.Fatal(err)
		}
		cfg := config.Default()
		cfg.StoreDir = filepath.Join(dir, "store")
		cfg.Encryption.Mode = mode
		cfg.Encryption.KeyFile = keyFile
		cfg.Encryption.PassphraseEnv = "WA_MCP_TEST_LOG_PASS"
		cfg.Encryption.EFS = false
		cfg.API.Token = strings.Repeat("7e", 32)
		cfg.Webhooks = []config.Webhook{{URL: "http://127.0.0.1:1/hook", SecretEnv: "WA_MCP_TEST_HOOK"}}
		t.Setenv("WA_MCP_TEST_LOG_PASS", passphrase)
		t.Setenv("WA_MCP_TEST_HOOK", webhookSecret)

		// A plain store with a message is migrated at the first start.
		plain, err := store.Open(cfg.MessagesDB())
		if err != nil {
			t.Fatal(err)
		}
		plain.PutMessage(ctx, &store.Message{Conv: "15550100002@s.whatsapp.net", ID: "OLD", SentAt: 1, Body: text})
		plain.Close()

		db, err := openStore(ctx, cfg, func(f string, a ...any) { logger.Printf("debug: "+f, a...) })
		if err != nil {
			t.Fatalf("%s: %v", mode, err)
		}
		logKeySetup(logger, cfg, db, 4)
		logger.Printf("api token: %s", api.TokenSource(cfg))
		hooks, notes := webhook.New(cfg.Webhooks, os.Getenv, webhook.Options{Log: logger})
		for _, n := range notes {
			logger.Printf("warning: %s", n)
		}
		mon := &security.Monitor{Store: db, Log: logger, Emit: hooks.Emit}
		mon.SetProtection(protection(cfg, db, protectFolder(cfg, logger)))
		mon.Record(security.SessionReplaced, "another client connected with the keys of this session")
		in := &wa.Ingest{DB: db, Log: logger,
			Self: func() (types.JID, types.JID) {
				return types.NewJID("15550100001", types.DefaultUserServer), types.JID{}
			}}
		evt := &events.Message{Info: types.MessageInfo{
			MessageSource: types.MessageSource{Chat: types.NewJID("15550100002", types.DefaultUserServer),
				Sender: types.NewJID("15550100002", types.DefaultUserServer)},
			ID: "NEW1", Timestamp: time.Unix(1_800_000_000, 0), PushName: "Alice"},
			Message: &waE2E.Message{Conversation: proto.String(text)}}
		if _, err := in.Message(ctx, evt, true); err != nil {
			t.Fatal(err)
		}
		if m, err := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "OLD"); err != nil || m.Body != text {
			t.Fatalf("%s: migrated message %v", mode, err)
		}
		db.Close()

		if !strings.Contains(buf.String(), "encrypted 1 existing fields") {
			t.Fatalf("%s: no migration note in the log:\n%s", mode, buf.String())
		}
		for _, secret := range []string{text, "4711", passphrase, webhookSecret, cfg.API.Token, hex.EncodeToString(key)} {
			if strings.Contains(buf.String(), secret) {
				t.Fatalf("%s: the log contains %q:\n%s", mode, secret, buf.String())
			}
		}
		if mk, err := testKeystore.Get(keystore.ItemStoreKey); err == nil && strings.Contains(buf.String(), hex.EncodeToString(mk)) {
			t.Fatalf("%s: the log contains the store key", mode)
		}
	}
}

func TestEncryptedStoreWithModeOffIsRefused(t *testing.T) {
	dir := t.TempDir()
	cfg := config.Default()
	cfg.StoreDir = filepath.Join(dir, "store")
	db, err := openStore(context.Background(), cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	if db.KeyKind() != store.KindKeystore {
		t.Fatalf("default kind %q", db.KeyKind())
	}
	db.Close()
	cfg.Encryption.Mode = config.ModeOff
	if _, err := openStore(context.Background(), cfg, nil); err == nil || !strings.Contains(err.Error(), "encryption.mode is off") {
		t.Fatalf("mode off: %v", err)
	}
}

func TestAPITokenCommand(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store", "encryption": {"mode": "off"}}`), 0o600)
	var out, errOut bytes.Buffer
	if code := run([]string{"api-token", "--config", cfgPath}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "start the bridge once") {
		t.Fatalf("no token: %d %q", code, errOut.String())
	}
	os.MkdirAll(filepath.Join(dir, "store"), 0o700)
	tok := strings.Repeat("ab", 32)
	os.WriteFile(filepath.Join(dir, "store", "api.token"), []byte(tok+"\n"), 0o600)
	out.Reset()
	if code := run([]string{"api-token", "--config", cfgPath}, &out, &errOut); code != 0 || strings.TrimSpace(out.String()) != tok {
		t.Fatalf("token: %d %q", code, out.String())
	}
}

func TestPassphraseFromFile(t *testing.T) {
	cfg := config.Default()
	cfg.StoreDir = t.TempDir()
	cfg.Encryption.Mode = config.ModePassphrase
	good := filepath.Join(t.TempDir(), "pass")
	if err := fsutil.WriteFile(good, []byte("correct horse battery\n")); err != nil {
		t.Fatal(err)
	}
	env := func(file string) func(string) string {
		return func(name string) string {
			if name == "WA_MCP_PASSPHRASE_FILE" {
				return file
			}
			return ""
		}
	}
	got, err := keyPlan(cfg, keystore.NewMemory(), env(good)).Secret(store.KindPassphrase, true)
	if err != nil || string(got) != "correct horse battery" {
		t.Fatalf("passphrase file: %q %v", got, err)
	}
	open := filepath.Join(t.TempDir(), "open")
	os.WriteFile(open, []byte("x"), 0o644)
	if _, err := keyPlan(cfg, keystore.NewMemory(), env(open)).Secret(store.KindPassphrase, true); err == nil ||
		!strings.Contains(err.Error(), "WA_MCP_PASSPHRASE_FILE") {
		t.Fatalf("readable file: %v", err)
	}
}

func TestKeyErrorsExitWithTheFatalCode(t *testing.T) {
	dir := t.TempDir()
	cfg := config.Default()
	cfg.StoreDir = filepath.Join(dir, "store")
	db, err := openStore(context.Background(), cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()
	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"store_dir": "store", "encryption": {"mode": "off", "efs": false}}`), 0o600)
	var out, errOut bytes.Buffer
	if code := run([]string{"--config", cfgPath}, &out, &errOut); code != ExitFatal || !strings.Contains(out.String(), "encryption.mode is off") {
		t.Fatalf("exit %d, %q", code, out.String())
	}
	if !IsFatal(fatal(errCopiedStore)) || IsFatal(keystore.ErrUnavailable) {
		t.Fatal("IsFatal")
	}
}

func TestSessionFolderOutsideTheStore(t *testing.T) {
	dir := t.TempDir()
	cfg := config.Default()
	cfg.StoreDir = filepath.Join(dir, "store")
	cfg.SessionFile = filepath.Join(dir, "session-volume", "session.db")
	cfg.Encryption.EFS = false
	logger := log.New(io.Discard, "", 0)
	if !sessionOutsideStore(cfg) {
		t.Fatal("outside session not detected")
	}
	if err := prepareSessionDir(cfg, logger); err != nil {
		t.Fatal(err)
	}
	if st, err := os.Stat(filepath.Dir(cfg.SessionDB())); err != nil || !st.IsDir() {
		t.Fatalf("folder: %v", err)
	}
	os.WriteFile(cfg.SessionDB(), []byte("x"), 0o644)
	restrictSession(cfg, logger)
	if runtime.GOOS != "windows" {
		if st, _ := os.Stat(cfg.SessionDB()); st.Mode().Perm() != 0o600 {
			t.Fatalf("mode %o", st.Mode().Perm())
		}
		if st, _ := os.Stat(filepath.Dir(cfg.SessionDB())); st.Mode().Perm() != 0o700 {
			t.Fatalf("folder mode %o", st.Mode().Perm())
		}
	} else if _, err := keystore.ReadSecretFile(cfg.SessionDB()); err != nil {
		t.Fatalf("the session file is not limited to the user: %v", err)
	}
}
