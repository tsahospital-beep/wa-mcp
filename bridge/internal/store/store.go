// Package store owns the bridge database (store/wa.db): schema, migrations,
// queries, optional field sealing and the legacy import.
package store

import (
	"context"
	"crypto/rand"
	"database/sql"
	"database/sql/driver"
	"encoding/base64"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	sqlite "github.com/glebarez/go-sqlite"
	"golang.org/x/text/cases"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/crypt"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

var (
	ErrNotFound        = errors.New("not found")
	ErrEncryptedStore  = errors.New("the database is encrypted: encryption.mode must not be off")
	ErrNoPassphrase    = errors.New("encryption is enabled but the passphrase variable is empty")
	ErrWrongPassphrase = errors.New("wrong passphrase for this database")
	ErrNewerSchema     = errors.New("the database was written by a newer version")
)

// folder applies full Unicode case folding (so "STRASSE" matches a sharp s),
// the same as Python's str.casefold.
var folder = cases.Fold()

// Fold is the case folding used for text search (SQL function wa_fold).
func Fold(s string) string { return folder.String(s) }

func init() {
	sqlite.MustRegisterDeterministicScalarFunction("wa_fold", 1, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		switch v := args[0].(type) {
		case string:
			return Fold(v), nil
		case []byte:
			return Fold(string(v)), nil
		}
		return "", nil
	})
}

// DB is the bridge database handle.
type DB struct {
	sql  *sql.DB
	seal *crypt.Sealer
	path string
	// Debug receives debug-level notes (never message text). May be nil.
	Debug func(format string, args ...any)

	// Now is the clock for pruning; nil means time.Now.
	Now func() time.Time

	keyMu     sync.Mutex
	outboxKey []byte
	keyState  KeyState // result of SetupKey

	pruneMu      sync.Mutex
	lastCapPrune int64 // unix time of the last reaction count cap

	observer observerSlot // see observe.go
}

func (d *DB) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now()
}

// JournalSizeLimit caps the WAL file left after a checkpoint (64 MiB), for wa.db and session.db.
const JournalSizeLimit = "67108864"

// Open creates or opens the database at path and applies pending migrations.
func Open(path string) (*DB, error) {
	if err := fsutil.MkdirPrivate(filepath.Dir(path)); err != nil {
		return nil, err
	}
	dsn := path + "?_pragma=journal_mode(WAL)&_pragma=busy_timeout(8000)&_pragma=synchronous(NORMAL)&_pragma=journal_size_limit(" + JournalSizeLimit + ")"
	conn, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// One connection keeps writes ordered and avoids lock errors inside the process.
	conn.SetMaxOpenConns(1)
	d := &DB{sql: conn, path: path}
	if err := d.migrate(context.Background()); err != nil {
		conn.Close()
		return nil, err
	}
	return d, nil
}

// Close closes the database.
func (d *DB) Close() error { return d.sql.Close() }

// Path returns the database file path.
func (d *DB) Path() string { return d.path }

// SQL exposes the handle for tests and maintenance commands.
func (d *DB) SQL() *sql.DB { return d.sql }

// migrations[i] moves the schema from version i to i+1.
var migrations = []string{
	`
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE conversations (
  jid TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', is_group INTEGER NOT NULL DEFAULT 0,
  last_activity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE chat_messages (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', outgoing INTEGER NOT NULL DEFAULT 0,
  sent_at INTEGER NOT NULL, body TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'text',
  attachment_name TEXT, mime TEXT, byte_size INTEGER,
  direct_path TEXT, cdn_url TEXT, media_key BLOB, plain_sha256 BLOB, enc_sha256 BLOB, local_path TEXT,
  reply_to_id TEXT, reply_to_author TEXT, reply_preview TEXT,
  mentioned TEXT,
  read_at INTEGER,
  PRIMARY KEY (conv, msg_id));
CREATE INDEX chat_messages_conv_time ON chat_messages(conv, sent_at);
CREATE INDEX chat_messages_time ON chat_messages(sent_at);
CREATE TABLE derived_text (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, source TEXT NOT NULL,
  lang TEXT, confidence REAL, content TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (conv, msg_id, source));
CREATE TABLE call_log (
  call_id TEXT PRIMARY KEY, peer TEXT NOT NULL, group_jid TEXT, outgoing INTEGER NOT NULL, video INTEGER NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE scheduled_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', media_path TEXT,
  reply_to_id TEXT, mentions TEXT, next_run INTEGER NOT NULL, recurrence TEXT NOT NULL DEFAULT 'once',
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at INTEGER NOT NULL, last_sent_at INTEGER);
CREATE TABLE outbox_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, body_hash TEXT NOT NULL, sent_at INTEGER NOT NULL);
`,
	// Day of month a monthly schedule was created for, so Jan 31 -> Feb 28 -> Mar 31.
	`
ALTER TABLE scheduled_messages ADD COLUMN anchor_day INTEGER;
CREATE INDEX outbox_log_target_time ON outbox_log(target, sent_at);
CREATE INDEX scheduled_messages_due ON scheduled_messages(status, next_run);
`,
	// Edits, deletions, reactions, changes that arrive before their message,
	// and the kind and pacing flag of scheduled sends.
	`
ALTER TABLE chat_messages ADD COLUMN edited_at INTEGER;
ALTER TABLE chat_messages ADD COLUMN deleted_at INTEGER;
ALTER TABLE scheduled_messages ADD COLUMN kind TEXT;
ALTER TABLE scheduled_messages ADD COLUMN human INTEGER;
CREATE TABLE pending_changes (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, kind TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '', actor_alt TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  at INTEGER NOT NULL, PRIMARY KEY (conv, msg_id, kind));
CREATE TABLE reactions (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, reactor TEXT NOT NULL, emoji TEXT NOT NULL,
  reacted_at INTEGER NOT NULL, PRIMARY KEY (conv, msg_id, reactor));
`,
	// Deletions by someone other than the author that could not be confirmed
	// (kept visible with a flag), and insert times for pruning.
	`
ALTER TABLE chat_messages ADD COLUMN deleted_unverified INTEGER;
ALTER TABLE chat_messages ADD COLUMN deleted_by TEXT;
ALTER TABLE pending_changes ADD COLUMN added_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reactions ADD COLUMN added_at INTEGER NOT NULL DEFAULT 0;
CREATE INDEX pending_changes_added ON pending_changes(added_at);
`,
	// Names and the LID map copied from session.db, so readers never need the
	// session file; an index for reaction pruning; the oldest reader that
	// understands this schema (wae2 values, wa_contacts).
	`
CREATE TABLE wa_contacts (
  jid TEXT PRIMARY KEY, first_name TEXT NOT NULL DEFAULT '', full_name TEXT NOT NULL DEFAULT '',
  push_name TEXT NOT NULL DEFAULT '', business_name TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE wa_lid_map (lid TEXT PRIMARY KEY, pn TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT 0);
CREATE INDEX wa_lid_map_pn ON wa_lid_map(pn);
CREATE INDEX reactions_added ON reactions(added_at);
INSERT INTO meta (key, value) VALUES ('min_reader_schema', '5') ON CONFLICT (key) DO UPDATE SET value = excluded.value;
`,
}

// MinReaderSchema is written to meta.min_reader_schema: readers (the Python
// package) that know an older schema must not read this database.
const MinReaderSchema = 5

// LatestSchema is the schema version this build writes.
var LatestSchema = len(migrations)

func (d *DB) migrate(ctx context.Context) error {
	if _, err := d.sql.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`); err != nil {
		return fmt.Errorf("create meta: %w", err)
	}
	current, err := d.SchemaVersion(ctx)
	if err != nil {
		return err
	}
	if current > len(migrations) {
		return fmt.Errorf("%w: %s has schema %d, but this bridge knows schema %d at most. "+
			"Update the bridge; it does not open newer databases, so nothing was changed", ErrNewerSchema, d.path, current, len(migrations))
	}
	for v := current; v < len(migrations); v++ {
		tx, err := d.sql.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, migrations[v]); err != nil {
			tx.Rollback()
			return fmt.Errorf("migration to schema %d: %w", v+1, err)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO meta (key, value) VALUES ('schema_version', ?)
			ON CONFLICT (key) DO UPDATE SET value = excluded.value`, strconv.Itoa(v+1)); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

// SchemaVersion returns meta.schema_version (0 for a new file).
func (d *DB) SchemaVersion(ctx context.Context) (int, error) {
	v, err := d.Meta(ctx, "schema_version")
	if errors.Is(err, ErrNotFound) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0, fmt.Errorf("bad schema_version %q", v)
	}
	return n, nil
}

// Meta reads one meta value.
func (d *DB) Meta(ctx context.Context, key string) (string, error) {
	var v string
	err := d.sql.QueryRowContext(ctx, `SELECT value FROM meta WHERE key = ?`, key).Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotFound
	}
	return v, err
}

// SetMeta writes one meta value.
func (d *DB) SetMeta(ctx context.Context, key, value string) error {
	_, err := d.sql.ExecContext(ctx, `INSERT INTO meta (key, value) VALUES (?, ?)
		ON CONFLICT (key) DO UPDATE SET value = excluded.value`, key, value)
	return err
}

// AccountJID returns the account this store belongs to ("" before the first login).
func (d *DB) AccountJID(ctx context.Context) (string, error) {
	v, err := d.Meta(ctx, "account_jid")
	if errors.Is(err, ErrNotFound) {
		return "", nil
	}
	return v, err
}

// SetAccountJID records the account this store belongs to.
func (d *DB) SetAccountJID(ctx context.Context, jid string) error {
	return d.SetMeta(ctx, "account_jid", jid)
}

// SetupEncryption is SetupKey for the passphrase kind only: enabled=true
// opens a passphrase database or encrypts a plain one, enabled=false refuses
// an encrypted database.
func (d *DB) SetupEncryption(ctx context.Context, enabled bool, passphrase string) error {
	u := KeyPlan{Secret: func(kind string, create bool) ([]byte, error) {
		if kind != KindPassphrase {
			return nil, fmt.Errorf("this database is encrypted with a %s key, not a passphrase", kind)
		}
		if passphrase == "" {
			return nil, ErrNoPassphrase
		}
		return []byte(passphrase), nil
	}}
	if enabled {
		u.NewKind = KindPassphrase
	}
	_, err := d.SetupKey(ctx, u)
	return err
}

func (d *DB) setSealer(s *crypt.Sealer) {
	d.keyMu.Lock()
	d.seal, d.outboxKey = s, nil
	d.keyMu.Unlock()
}

// Encrypted reports whether field sealing is active.
func (d *DB) Encrypted() bool { return d.seal != nil }

// OutboxKey is the HMAC key for outbox_log.body_hash: derived from the store
// key when encryption is on, otherwise a random key kept in meta.
func (d *DB) OutboxKey(ctx context.Context) ([]byte, error) {
	d.keyMu.Lock()
	defer d.keyMu.Unlock()
	if d.outboxKey != nil {
		return d.outboxKey, nil
	}
	if d.seal != nil {
		d.outboxKey = d.seal.Subkey("outbox")
		return d.outboxKey, nil
	}
	v, err := d.Meta(ctx, "outbox_key")
	if err == nil {
		if key, err := base64.StdEncoding.DecodeString(v); err == nil && len(key) >= 16 {
			d.outboxKey = key
			return key, nil
		}
	} else if !errors.Is(err, ErrNotFound) {
		return nil, err
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	if err := d.SetMeta(ctx, "outbox_key", base64.StdEncoding.EncodeToString(key)); err != nil {
		return nil, err
	}
	d.outboxKey = key
	return key, nil
}

func (d *DB) debugf(format string, args ...any) {
	if d.Debug != nil {
		d.Debug(format, args...)
	}
}

// field names the place of a sealed value; it becomes the associated data
// of "wae2:" values (crypt.AAD), so a value copied to another row or column
// does not open.
type field struct{ table, column, conv, id string }

func (f field) aad() []byte { return crypt.AAD(f.table, f.column, f.conv, f.id) }

// sealText seals every non-empty value while encryption is on, so stored text
// that happens to start with a prefix still round-trips.
func (d *DB) sealText(s string, f field) (string, error) {
	if d.seal == nil || s == "" {
		return s, nil
	}
	return d.seal.SealAAD([]byte(s), f.aad())
}

// openText never unseals without encryption; a value that fails to open is
// returned as stored.
func (d *DB) openText(s string, f field) string {
	if d.seal == nil || !crypt.IsSealed(s) {
		return s
	}
	v, err := d.seal.OpenAAD(s, f.aad())
	if err != nil {
		d.debugf("store: could not unseal a %s.%s value; returning it as stored", f.table, f.column)
		return s
	}
	return string(v)
}

func (d *DB) sealBlob(b []byte, f field) ([]byte, error) {
	if d.seal == nil || len(b) == 0 {
		return b, nil
	}
	v, err := d.seal.SealAAD(b, f.aad())
	return []byte(v), err
}

func (d *DB) openBlob(b []byte, f field) []byte {
	if d.seal == nil || !crypt.IsSealed(string(b)) {
		return b
	}
	v, err := d.seal.OpenAAD(string(b), f.aad())
	if err != nil {
		d.debugf("store: could not unseal a %s.%s value; returning it as stored", f.table, f.column)
		return b
	}
	return v
}

// nullable helpers: empty values are stored as NULL.
func nullText(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func nullInt(n int64) any {
	if n == 0 {
		return nil
	}
	return n
}

func nullBlob(b []byte) any {
	if len(b) == 0 {
		return nil
	}
	return b
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
