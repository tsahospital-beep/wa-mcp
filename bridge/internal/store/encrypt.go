package store

import (
	"bytes"
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/base64"
	"errors"
	"fmt"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/crypt"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// Key kinds recorded in meta.crypt_kind. A sealed database without
// crypt_kind is a passphrase database written before kinds existed.
const (
	KindPassphrase = "passphrase"
	KindKeystore   = "keystore"
	KindKeyfile    = "keyfile"
)

// ErrWrongKey means the store key (OS keystore or key file) does not open the database.
var ErrWrongKey = errors.New("the store key does not open this database")

// KeyPlan tells SetupKey where keys come from.
type KeyPlan struct {
	// NewKind is the kind used to encrypt a database that is still plain.
	// "" keeps a plain database plain and refuses an encrypted one.
	NewKind string
	// Secret returns the passphrase (KindPassphrase) or the 32-byte store key
	// of kind. create is set when a plain database is about to be encrypted,
	// so a missing store key may be made now.
	Secret func(kind string, create bool) ([]byte, error)
}

// KeyState is the result of SetupKey.
type KeyState struct {
	// Kind is the key kind in use, "" for a plain database.
	Kind string
	// Migrated counts the fields that were sealed by an in-place migration.
	Migrated int
	// KeptBackup is a plain copy made before a migration that could not be removed.
	KeptBackup string
	// CompactPending is set while the file may still hold plain text in free
	// pages or the WAL because compaction did not finish; the next start retries.
	CompactPending bool
}

// sealedColumns are the fields the store seals while encryption is on, with
// the columns that make up their associated data (crypt.AAD).
var sealedColumns = []struct {
	table, column string
	conv, id      string // key columns for the AAD; "" means an empty part
	extra         string // a further key column appended to the AAD ("" = none)
	blob          bool
}{
	{"chat_messages", "body", "conv", "msg_id", "", false},
	{"chat_messages", "attachment_name", "conv", "msg_id", "", false},
	{"chat_messages", "reply_preview", "conv", "msg_id", "", false},
	{"chat_messages", "media_key", "conv", "msg_id", "", true},
	{"pending_changes", "body", "conv", "msg_id", "", false},
	{"reactions", "emoji", "conv", "msg_id", "", false},
	{"scheduled_messages", "body", "target", "", "", false},
	{"derived_text", "content", "conv", "msg_id", "source", false},
	{"wa_contacts", "first_name", "jid", "", "", false},
	{"wa_contacts", "full_name", "jid", "", "", false},
	{"wa_contacts", "push_name", "jid", "", "", false},
	{"wa_contacts", "business_name", "jid", "", "", false},
}

// BackupSuffix names the plain copy made before an in-place migration.
const BackupSuffix = ".pre-encryption"

// metaCompactPending is set while compaction after a migration is still due.
const metaCompactPending = "compact_pending"

func fieldSealer(kind string, secret, salt []byte) (*crypt.Sealer, error) {
	switch kind {
	case KindPassphrase:
		return crypt.NewSealer(crypt.DeriveKey(string(secret), salt))
	case KindKeystore, KindKeyfile:
		k, err := crypt.FieldKey(secret, salt)
		if err != nil {
			return nil, err
		}
		return crypt.NewSealer(k)
	}
	return nil, fmt.Errorf("unknown key kind %q in meta.crypt_kind", kind)
}

func (d *DB) metaOrEmpty(ctx context.Context, key string) (string, error) {
	v, err := d.Meta(ctx, key)
	if errors.Is(err, ErrNotFound) {
		return "", nil
	}
	return v, err
}

// SetupKey opens an encrypted database with the key of its recorded kind, or
// encrypts a plain one in place when u.NewKind is set. Before a plain database
// with data is migrated, a plain copy is written next to it. The migration
// holds an exclusive lock on the file, runs in one transaction and opens
// every sealed value again; the copy is then wiped and the file compacted, so
// no plain text stays in free pages or the WAL. A copy or a compaction left
// over by an interrupted start is finished at the next start.
func (d *DB) SetupKey(ctx context.Context, u KeyPlan) (KeyState, error) {
	saltText, err := d.metaOrEmpty(ctx, "crypt_salt")
	if err != nil {
		return KeyState{}, err
	}
	if saltText == "" && u.NewKind == "" {
		d.setSealer(nil)
		d.setState(KeyState{})
		return KeyState{}, nil
	}
	var st KeyState
	if saltText != "" {
		st, err = d.openSealed(ctx, u, saltText)
		if err != nil {
			return st, err
		}
		d.finishLeftovers(ctx, &st)
	} else if st, err = d.encryptPlain(ctx, u); err != nil {
		return st, err
	}
	d.setState(st)
	return st, nil
}

// finishLeftovers wipes a plain copy left by an interrupted migration and
// retries a compaction that did not finish.
func (d *DB) finishLeftovers(ctx context.Context, st *KeyState) {
	if err := fsutil.Wipe(d.path + BackupSuffix); err != nil {
		st.KeptBackup = d.path + BackupSuffix
	}
	if v, _ := d.metaOrEmpty(ctx, metaCompactPending); v == "" {
		return
	}
	err := d.withExclusive(ctx, func(c *sql.Conn) error {
		if err := compact(ctx, c); err != nil {
			return err
		}
		_, err := c.ExecContext(ctx, `DELETE FROM meta WHERE key = ?`, metaCompactPending)
		return err
	})
	if err != nil {
		d.debugf("store: compaction is still pending: %v", err)
		st.CompactPending = true
	}
}

func (d *DB) openSealed(ctx context.Context, u KeyPlan, saltText string) (KeyState, error) {
	if u.NewKind == "" {
		return KeyState{}, ErrEncryptedStore
	}
	kind, err := d.metaOrEmpty(ctx, "crypt_kind")
	if err != nil {
		return KeyState{}, err
	}
	if kind == "" {
		kind = KindPassphrase
	}
	salt, err := base64.StdEncoding.DecodeString(saltText)
	if err != nil {
		return KeyState{}, fmt.Errorf("meta.crypt_salt is damaged: %w", err)
	}
	secret, err := u.Secret(kind, false)
	if err != nil {
		return KeyState{Kind: kind}, err
	}
	s, err := fieldSealer(kind, secret, salt)
	if err != nil {
		return KeyState{Kind: kind}, err
	}
	check, err := d.Meta(ctx, "crypt_check")
	if err != nil {
		return KeyState{Kind: kind}, fmt.Errorf("meta.crypt_check: %w", err)
	}
	if !s.VerifyCheck(check) {
		if kind == KindPassphrase {
			return KeyState{Kind: kind}, ErrWrongPassphrase
		}
		return KeyState{Kind: kind}, ErrWrongKey
	}
	d.setSealer(s)
	return KeyState{Kind: kind}, nil
}

func (d *DB) encryptPlain(ctx context.Context, u KeyPlan) (KeyState, error) {
	kind := u.NewKind
	st := KeyState{Kind: kind}
	secret, err := u.Secret(kind, true)
	if err != nil {
		return st, err
	}
	salt, err := crypt.NewSalt()
	if err != nil {
		return st, err
	}
	s, err := fieldSealer(kind, secret, salt)
	if err != nil {
		return st, err
	}
	check, err := s.MakeCheck()
	if err != nil {
		return st, err
	}
	plain, err := d.countSealable(ctx)
	if err != nil {
		return st, err
	}
	backup := ""
	if plain > 0 {
		if backup, err = d.backupCopy(ctx); err != nil {
			return st, fmt.Errorf("backup before encryption: %w", err)
		}
	}
	err = d.withExclusive(ctx, func(c *sql.Conn) error {
		n, err := migrateTx(ctx, c, s, map[string]string{
			"crypt_kind": kind, "crypt_salt": base64.StdEncoding.EncodeToString(salt), "crypt_check": check,
		})
		if err != nil {
			return fmt.Errorf("encrypting existing rows (nothing was changed; the copy %s is kept): %w", backup, err)
		}
		st.Migrated = n
		d.setSealer(s)
		// The database is sealed and checked: the plain copy goes before the slower compaction.
		if backup != "" {
			if err := fsutil.Wipe(backup); err != nil {
				st.KeptBackup = backup
			}
		}
		if plain == 0 {
			return nil
		}
		if err := compact(ctx, c); err != nil {
			d.debugf("store: compacting after encryption did not finish: %v", err)
			st.CompactPending = true
			_, err = c.ExecContext(ctx, `INSERT INTO meta (key, value) VALUES (?, '1')
				ON CONFLICT (key) DO UPDATE SET value = excluded.value`, metaCompactPending)
			return err
		}
		return nil
	})
	return st, err
}

// exclusiveWait bounds how long withExclusive waits for other connections to close.
var exclusiveWait = 2 * time.Second

// withExclusive runs fn on one connection. It first tries exclusive locking
// mode, which keeps other programs (such as the MCP server) from reading
// while plain text is rewritten and from holding back checkpoints. In WAL
// mode that lock needs every other connection to be closed; when one stays
// open, fn runs with normal locking, where the transaction still keeps
// readers on the old snapshot and a held-back checkpoint is retried later.
// Afterwards the connection is closed, which gives any lock back.
func (d *DB) withExclusive(ctx context.Context, fn func(c *sql.Conn) error) error {
	c, err := d.sql.Conn(ctx)
	if err != nil {
		return err
	}
	defer c.Close()
	// In WAL mode a read does not give an exclusive lock back; closing the
	// connection does. The pool then opens a fresh one with the usual pragmas.
	defer c.Raw(func(any) error { return driver.ErrBadConn })
	var v string
	c.QueryRowContext(ctx, fmt.Sprintf(`PRAGMA busy_timeout = %d`, exclusiveWait.Milliseconds())).Scan(&v)
	if err := c.QueryRowContext(ctx, `PRAGMA locking_mode = EXCLUSIVE`).Scan(&v); err != nil {
		return err
	}
	// The lock is taken by a write transaction and kept after its commit.
	_, err = c.ExecContext(ctx, `BEGIN EXCLUSIVE`)
	if err == nil {
		_, err = c.ExecContext(ctx, `COMMIT`)
	}
	if err != nil {
		d.debugf("store: another program has the database open; continuing without an exclusive lock")
		c.ExecContext(ctx, `ROLLBACK`)
		c.QueryRowContext(ctx, `PRAGMA locking_mode = NORMAL`).Scan(&v)
	}
	c.QueryRowContext(ctx, `PRAGMA busy_timeout = 8000`).Scan(&v)
	return fn(c)
}

// migrateTx seals every plain value and writes the key meta in one write transaction.
func migrateTx(ctx context.Context, c *sql.Conn, s *crypt.Sealer, meta map[string]string) (n int, err error) {
	if _, err := c.ExecContext(ctx, `BEGIN IMMEDIATE`); err != nil {
		return 0, fmt.Errorf("the database is in use by another program: %w", err)
	}
	defer func() {
		if err != nil {
			c.ExecContext(context.Background(), `ROLLBACK`)
		}
	}()
	for k, v := range meta {
		if _, err := c.ExecContext(ctx, `INSERT INTO meta (key, value) VALUES (?, ?)`, k, v); err != nil {
			return 0, err
		}
	}
	if n, err = sealAll(ctx, c, s); err != nil {
		return 0, err
	}
	// Hashes made with the stored random key would let a reader test guesses;
	// from now on the key comes from the store key.
	if _, err := c.ExecContext(ctx, `UPDATE outbox_log SET body_hash = ''`); err != nil {
		return 0, err
	}
	if _, err := c.ExecContext(ctx, `DELETE FROM meta WHERE key = 'outbox_key'`); err != nil {
		return 0, err
	}
	if _, err := c.ExecContext(ctx, `COMMIT`); err != nil {
		return 0, err
	}
	return n, nil
}

// countSealable counts non-empty values in sealed columns.
func (d *DB) countSealable(ctx context.Context) (int, error) {
	total := 0
	for _, c := range sealedColumns {
		var n int
		q := fmt.Sprintf(`SELECT COUNT(*) FROM %s WHERE %s IS NOT NULL AND length(%s) > 0`, c.table, c.column, c.column)
		if err := d.sql.QueryRowContext(ctx, q).Scan(&n); err != nil {
			return 0, err
		}
		total += n
	}
	return total, nil
}

// backupCopy writes a consistent plain copy of the database next to it.
func (d *DB) backupCopy(ctx context.Context) (string, error) {
	path := d.path + BackupSuffix
	// A copy left by an earlier failed attempt is older than the still plain database.
	if err := fsutil.Wipe(path); err != nil {
		return "", err
	}
	if _, err := d.sql.ExecContext(ctx, `VACUUM INTO ?`, path); err != nil {
		return "", err
	}
	if err := fsutil.Restrict(path); err != nil {
		return path, err
	}
	return path, nil
}

const migrateBatch = 500

type execQuerier interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

func sealAll(ctx context.Context, ex execQuerier, s *crypt.Sealer) (int, error) {
	total := 0
	for _, c := range sealedColumns {
		idCol, extraCol := "''", "''"
		if c.id != "" {
			idCol = c.id
		}
		if c.extra != "" {
			extraCol = c.extra
		}
		q := fmt.Sprintf(`SELECT rowid, %s, %s, %s, %s FROM %s WHERE rowid > ? AND %s IS NOT NULL AND length(%s) > 0 ORDER BY rowid LIMIT %d`,
			c.column, c.conv, idCol, extraCol, c.table, c.column, c.column, migrateBatch)
		upd := fmt.Sprintf(`UPDATE %s SET %s = ? WHERE rowid = ?`, c.table, c.column)
		for last := int64(0); ; {
			type row struct {
				id              int64
				val             []byte
				conv, mk, extra string
			}
			var batch []row
			rows, err := ex.QueryContext(ctx, q, last)
			if err != nil {
				return 0, err
			}
			for rows.Next() {
				var r row
				if err := rows.Scan(&r.id, &r.val, &r.conv, &r.mk, &r.extra); err != nil {
					rows.Close()
					return 0, err
				}
				batch = append(batch, r)
			}
			rows.Close()
			if err := rows.Err(); err != nil {
				return 0, err
			}
			if len(batch) == 0 {
				break
			}
			for _, r := range batch {
				var more []string
				if c.extra != "" {
					more = []string{r.extra}
				}
				aad := crypt.AAD(c.table, c.column, r.conv, r.mk, more...)
				sealed, err := s.SealAAD(r.val, aad)
				if err != nil {
					return 0, err
				}
				if back, err := s.OpenAAD(sealed, aad); err != nil || !bytes.Equal(back, r.val) {
					return 0, fmt.Errorf("a sealed %s.%s value did not open again", c.table, c.column)
				}
				var v any = sealed
				if c.blob {
					v = []byte(sealed)
				}
				if _, err := ex.ExecContext(ctx, upd, v, r.id); err != nil {
					return 0, err
				}
				last = r.id
				total++
			}
		}
	}
	return total, nil
}

// Checkpoint retries: a reader inside a transaction holds back a TRUNCATE
// checkpoint, which waits up to the busy timeout each time. During
// compaction that timeout is checkpointWait instead of the usual 8 seconds.
var (
	checkpointTries = 5
	checkpointWait  = time.Second
)

// compact rewrites the file so free pages and the WAL hold no plain text. A
// checkpoint that reports busy is retried; it fails when it stays busy.
func compact(ctx context.Context, c *sql.Conn) error {
	var v string
	c.QueryRowContext(ctx, fmt.Sprintf(`PRAGMA busy_timeout = %d`, checkpointWait.Milliseconds())).Scan(&v)
	defer func() { c.QueryRowContext(context.Background(), `PRAGMA busy_timeout = 8000`).Scan(&v) }()
	if err := checkpoint(ctx, c); err != nil {
		return err
	}
	if _, err := c.ExecContext(ctx, `VACUUM`); err != nil {
		return err
	}
	return checkpoint(ctx, c)
}

func checkpoint(ctx context.Context, c *sql.Conn) error {
	for try := 0; ; try++ {
		var busy, logFrames, done int
		if err := c.QueryRowContext(ctx, `PRAGMA wal_checkpoint(TRUNCATE)`).Scan(&busy, &logFrames, &done); err != nil {
			return err
		}
		if busy == 0 {
			return nil
		}
		if try+1 >= checkpointTries {
			return fmt.Errorf("the WAL checkpoint stayed busy (%d of %d frames written)", done, logFrames)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(checkpointWait):
		}
	}
}

func (d *DB) setState(st KeyState) {
	d.keyMu.Lock()
	d.keyState = st
	d.keyMu.Unlock()
}

// KeyKind returns the key kind in use ("" for a plain database).
func (d *DB) KeyKind() string { return d.KeySetup().Kind }

// KeySetup returns the result of the last successful SetupKey.
func (d *DB) KeySetup() KeyState {
	d.keyMu.Lock()
	defer d.keyMu.Unlock()
	return d.keyState
}
