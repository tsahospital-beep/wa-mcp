package store

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const marker = "alice-secret-marker"

var testStoreKey = bytes.Repeat([]byte{0x42}, 32)

func keyPlan(kind string, key []byte) KeyPlan {
	return KeyPlan{NewKind: kind, Secret: func(k string, create bool) ([]byte, error) {
		if k == KindPassphrase {
			return []byte("pw"), nil
		}
		return key, nil
	}}
}

// fillPlain writes one value into every sealed column of a plain database.
func fillPlain(t *testing.T, db *DB) {
	t.Helper()
	conv := "15550100002@s.whatsapp.net"
	db.Now = func() time.Time { return time.Unix(1_800_000_000, 0) }
	if err := db.PutMessage(ctx, &Message{Conv: conv, ID: "M1", SentAt: 1_800_000_000, Body: marker + " body",
		AttachmentName: marker + ".pdf", ReplyPreview: marker + " quote", MediaKey: []byte(marker), Kind: "document"}); err != nil {
		t.Fatal(err)
	}
	if err := db.PutMessage(ctx, &Message{Conv: conv, ID: "M2", SentAt: 1_800_000_001, Body: "wae1:looks sealed but is not"}); err != nil {
		t.Fatal(err)
	}
	if err := db.AddPending(ctx, PendingChange{Conv: conv, ID: "LATER", Kind: "edit", Body: marker + " edit", At: 1_800_000_000}); err != nil {
		t.Fatal(err)
	}
	if err := db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "M1", Reactor: conv, Emoji: "ok " + marker[:8], At: 1_800_000_000}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.AddScheduled(ctx, &Scheduled{Target: conv, Body: marker + " scheduled", NextRun: 1, CreatedAt: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.SQL().Exec(`INSERT INTO derived_text VALUES (?, 'M1', 'document', 'en', NULL, ?, 1)`, conv, marker+" derived"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ReserveSent(ctx, conv, "plainhash", 1_800_000_000, 1); err != nil {
		t.Fatal(err)
	}
}

func rawFiles(t *testing.T, path string) []byte {
	t.Helper()
	var all []byte
	for _, p := range []string{path, path + "-wal", path + BackupSuffix} {
		if b, err := os.ReadFile(p); err == nil {
			all = append(all, b...)
		}
	}
	return all
}

func TestPlainStoreIsEncryptedInPlace(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	fillPlain(t, db)
	db.OutboxKey(ctx)
	if !bytes.Contains(rawFiles(t, path), []byte(marker)) {
		t.Fatal("test setup: the marker should be on disk before the migration")
	}
	st, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err != nil {
		t.Fatal(err)
	}
	if st.Kind != KindKeystore || st.Migrated != 9 || st.KeptBackup != "" || db.KeyKind() != KindKeystore || !db.Encrypted() {
		t.Fatalf("state %+v", st)
	}
	for _, c := range sealedColumns {
		rows, _ := db.SQL().Query(`SELECT ` + c.column + ` FROM ` + c.table + ` WHERE ` + c.column + ` IS NOT NULL AND length(` + c.column + `) > 0`)
		for rows.Next() {
			var v []byte
			rows.Scan(&v)
			if !bytes.HasPrefix(v, []byte("wae2:")) {
				t.Errorf("%s.%s not sealed", c.table, c.column)
			}
		}
		rows.Close()
	}
	if _, err := os.Stat(path + BackupSuffix); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("the plain copy was not removed: %v", err)
	}
	if bytes.Contains(rawFiles(t, path), []byte(marker)) {
		t.Fatal("plain text is still in the database file or its WAL")
	}
	if _, err := db.Meta(ctx, "outbox_key"); !errors.Is(err, ErrNotFound) {
		t.Fatal("the stored outbox key survived")
	}
	m, err := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "M1")
	if err != nil || m.Body != marker+" body" || m.AttachmentName != marker+".pdf" || string(m.MediaKey) != marker {
		t.Fatalf("read back: %+v %v", m, err)
	}
	if m2, _ := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "M2"); m2.Body != "wae1:looks sealed but is not" {
		t.Fatalf("prefix text: %q", m2.Body)
	}
	if list, _ := db.ListMessages(ctx, MessageQuery{Query: "SECRET-MARKER BODY"}); len(list) != 1 {
		t.Fatalf("search after migration: %d", len(list))
	}
	db.Close()

	db, _ = Open(path)
	defer db.Close()
	if _, err := db.SetupKey(ctx, keyPlan(KindKeystore, bytes.Repeat([]byte{1}, 32))); !errors.Is(err, ErrWrongKey) {
		t.Fatalf("wrong key: %v", err)
	}
	if _, err := db.SetupKey(ctx, KeyPlan{}); !errors.Is(err, ErrEncryptedStore) {
		t.Fatalf("mode off: %v", err)
	}
	// The recorded kind wins over the mode for new stores.
	st, err = db.SetupKey(ctx, keyPlan(KindKeyfile, testStoreKey))
	if err != nil || st.Kind != KindKeystore {
		t.Fatalf("reopen: %+v %v", st, err)
	}
	if s, _ := db.ListScheduled(ctx, ""); len(s) != 1 || s[0].Body != marker+" scheduled" {
		t.Fatalf("scheduled: %+v", s)
	}
}

func TestFailedMigrationKeepsPlainStoreAndCopy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	fillPlain(t, db)
	os.WriteFile(path+BackupSuffix, []byte("stale copy"), 0o600)
	db.SQL().Exec(`CREATE TRIGGER block BEFORE UPDATE ON reactions BEGIN SELECT RAISE(ABORT, 'blocked'); END`)
	_, err = db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err == nil || !strings.Contains(err.Error(), "nothing was changed") {
		t.Fatalf("failure: %v", err)
	}
	if db.Encrypted() {
		t.Fatal("sealer set after a failed migration")
	}
	if v, _ := db.metaOrEmpty(ctx, "crypt_salt"); v != "" {
		t.Fatal("salt kept after rollback")
	}
	if m, _ := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "M1"); m.Body != marker+" body" {
		t.Fatalf("plain row changed: %q", m.Body)
	}
	if b, err := os.ReadFile(path + BackupSuffix); err != nil || bytes.Equal(b, []byte("stale copy")) {
		t.Fatalf("copy: %v", err)
	}
	db.SQL().Exec(`DROP TRIGGER block`)
	if st, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey)); err != nil || st.Migrated == 0 {
		t.Fatalf("retry: %+v %v", st, err)
	}
	if _, err := os.Stat(path + BackupSuffix); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("copy left after the retry")
	}
}

func TestPassphraseStoreKeepsWorkingUnderKeystoreMode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, _ := Open(path)
	if err := db.SetupEncryption(ctx, true, "pw"); err != nil {
		t.Fatal(err)
	}
	db.PutMessage(ctx, &Message{Conv: "15550100002@s.whatsapp.net", ID: "P1", SentAt: 1, Body: "from Bob"})
	// Databases from before key kinds have no crypt_kind.
	db.SQL().Exec(`DELETE FROM meta WHERE key = 'crypt_kind'`)
	db.Close()

	db, _ = Open(path)
	defer db.Close()
	st, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err != nil || st.Kind != KindPassphrase {
		t.Fatalf("legacy passphrase store: %+v %v", st, err)
	}
	if m, _ := db.GetMessage(ctx, "15550100002@s.whatsapp.net", "P1"); m.Body != "from Bob" {
		t.Fatalf("body %q", m.Body)
	}
	wrong := KeyPlan{NewKind: KindKeystore, Secret: func(string, bool) ([]byte, error) { return []byte("other"), nil }}
	if _, err := db.SetupKey(ctx, wrong); !errors.Is(err, ErrWrongPassphrase) {
		t.Fatalf("wrong passphrase: %v", err)
	}
}

func TestNewEmptyStoreNeedsNoCopy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, _ := Open(path)
	defer db.Close()
	created := false
	u := KeyPlan{NewKind: KindKeyfile, Secret: func(kind string, create bool) ([]byte, error) {
		created = create
		return testStoreKey, nil
	}}
	st, err := db.SetupKey(ctx, u)
	if err != nil || st.Migrated != 0 || st.Kind != KindKeyfile || !created {
		t.Fatalf("%+v %v", st, err)
	}
	if v, _ := db.Meta(ctx, "crypt_kind"); v != KindKeyfile {
		t.Fatalf("kind %q", v)
	}
	failing := KeyPlan{NewKind: KindKeystore, Secret: func(string, bool) ([]byte, error) { return nil, errors.New("no keystore") }}
	other, _ := Open(filepath.Join(t.TempDir(), "wa.db"))
	defer other.Close()
	if _, err := other.SetupKey(ctx, failing); err == nil || other.Encrypted() {
		t.Fatal("a store without a key was encrypted")
	}
}
