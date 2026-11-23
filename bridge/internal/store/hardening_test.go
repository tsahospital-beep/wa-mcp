package store

import (
	"bytes"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/crypt"
)

// A crash after the commit but before the wipe left the plain copy; the next start removes it.
func TestLeftoverPlainCopyIsWipedAtNextStart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	fillPlain(t, db)
	if _, err := db.backupCopy(ctx); err != nil {
		t.Fatal(err)
	}
	saved, _ := os.ReadFile(path + BackupSuffix)
	if _, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey)); err != nil {
		t.Fatal(err)
	}
	db.Close()
	os.WriteFile(path+BackupSuffix, saved, 0o600) // what the crash left behind

	db2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	st, err := db2.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err != nil || st.KeptBackup != "" {
		t.Fatalf("%+v %v", st, err)
	}
	if _, err := os.Stat(path + BackupSuffix); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("the leftover plain copy is still there")
	}
}

// A reader in another connection holds back the checkpoint: compaction is
// marked pending, reported, and finished at the next start.
func TestCompactionBlockedByReaderIsRetried(t *testing.T) {
	oldTries, oldWait, oldEx := checkpointTries, checkpointWait, exclusiveWait
	checkpointTries, checkpointWait, exclusiveWait = 2, 10*time.Millisecond, 100*time.Millisecond
	t.Cleanup(func() { checkpointTries, checkpointWait, exclusiveWait = oldTries, oldWait, oldEx })

	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	fillPlain(t, db)
	db.Close()
	db, _ = Open(path)

	r, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := r.SQL().Begin()
	if err != nil {
		t.Fatal(err)
	}
	var n int
	tx.QueryRow(`SELECT COUNT(*) FROM chat_messages`).Scan(&n)
	st, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	tx.Rollback()
	r.Close()
	if err != nil || !st.CompactPending || st.KeptBackup != "" {
		t.Fatalf("with a reader inside a transaction: %+v %v", st, err)
	}
	if _, err := os.Stat(path + BackupSuffix); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("plain copy left")
	}
	if v, _ := db.Meta(ctx, metaCompactPending); v != "1" {
		t.Fatal("pending compaction not recorded")
	}
	db.Close()

	db, _ = Open(path)
	defer db.Close()
	st, err = db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err != nil || st.CompactPending {
		t.Fatalf("next start: %+v %v", st, err)
	}
	if _, err := db.Meta(ctx, metaCompactPending); !errors.Is(err, ErrNotFound) {
		t.Fatal("compact_pending not cleared")
	}
	if bytes.Contains(rawFiles(t, path), []byte(marker)) {
		t.Fatal("plain text left in the file or the WAL")
	}
}

func TestExclusiveLockKeepsReadersOutAndIsReleased(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, _ := Open(path)
	defer db.Close()
	reader := func() *sql.DB {
		r, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(50)")
		if err != nil {
			t.Fatal(err)
		}
		return r
	}
	var during error
	err := db.withExclusive(ctx, func(c *sql.Conn) error {
		if _, err := c.ExecContext(ctx, `INSERT INTO meta (key, value) VALUES ('probe', '1')`); err != nil {
			return err
		}
		r := reader()
		defer r.Close()
		var n int
		during = r.QueryRow(`SELECT COUNT(*) FROM meta`).Scan(&n)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if during == nil {
		t.Fatal("another connection could read while the exclusive lock was held")
	}
	r := reader()
	defer r.Close()
	var v string
	if err := r.QueryRow(`SELECT value FROM meta WHERE key = 'probe'`).Scan(&v); err != nil || v != "1" {
		t.Fatalf("after the lock: %q %v", v, err)
	}
	if err := db.SetMeta(ctx, "probe", "2"); err != nil {
		t.Fatalf("the pool connection after the lock: %v", err)
	}
}

// An idle connection of another program prevents the exclusive lock; the
// migration then runs with normal locking and still compacts.
func TestMigrationWithAnIdleReaderOpen(t *testing.T) {
	old := exclusiveWait
	exclusiveWait = 100 * time.Millisecond
	t.Cleanup(func() { exclusiveWait = old })
	path := filepath.Join(t.TempDir(), "wa.db")
	db, _ := Open(path)
	defer db.Close()
	fillPlain(t, db)
	r, _ := sql.Open("sqlite", path)
	defer r.Close()
	var n int
	r.QueryRow(`SELECT COUNT(*) FROM chat_messages`).Scan(&n)
	start := time.Now()
	st, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err != nil || st.CompactPending || st.Migrated == 0 {
		t.Fatalf("%+v %v", st, err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatalf("waited %s", time.Since(start))
	}
	if bytes.Contains(rawFiles(t, path), []byte(marker)) {
		t.Fatal("plain text left")
	}
}

func TestValuesAreBoundToTheirRow(t *testing.T) {
	db := openTemp(t)
	db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	conv := "15550100002@s.whatsapp.net"
	db.PutMessage(ctx, &Message{Conv: conv, ID: "S1", SentAt: 1, Body: "secret for Bob"})
	db.PutMessage(ctx, &Message{Conv: conv, ID: "S2", SentAt: 2, Body: "harmless"})
	// Someone with write access copies the sealed body of S1 into S2.
	db.SQL().Exec(`UPDATE chat_messages SET body = (SELECT body FROM chat_messages WHERE msg_id = 'S1') WHERE msg_id = 'S2'`)
	m, _ := db.GetMessage(ctx, conv, "S2")
	if m.Body == "secret for Bob" || !strings.HasPrefix(m.Body, "wae2:") {
		t.Fatalf("a moved value opened: %q", m.Body)
	}
	if m, _ := db.GetMessage(ctx, conv, "S1"); m.Body != "secret for Bob" {
		t.Fatalf("own row: %q", m.Body)
	}
}

func TestSchemaFiveDirectoryAndReaderVersion(t *testing.T) {
	db := openTemp(t)
	if v, _ := db.SchemaVersion(ctx); v != LatestSchema || LatestSchema < 5 {
		t.Fatalf("schema %d", v)
	}
	if v, _ := db.Meta(ctx, "min_reader_schema"); v != "5" {
		t.Fatalf("min_reader_schema %q", v)
	}
	var limit int
	db.SQL().QueryRow(`PRAGMA journal_size_limit`).Scan(&limit)
	if limit != 64<<20 {
		t.Fatalf("journal_size_limit %d", limit)
	}
	db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	alice := "15550100002@s.whatsapp.net"
	if err := db.PutContacts(ctx, []Contact{{JID: alice, FullName: "Alice Example", PushName: "Ally"}}); err != nil {
		t.Fatal(err)
	}
	db.PutContacts(ctx, []Contact{{JID: alice, FullName: "Alice Example", PushName: "Ally B"}})
	db.PutLIDPairs(ctx, []LIDPair{{LID: "200000000000002@lid", PN: alice}})
	db.SetOwnIDs(ctx, "15550100001@s.whatsapp.net", "200000000000001@lid")
	list, err := db.Contacts(ctx)
	if err != nil || len(list) != 1 || list[0].FullName != "Alice Example" || list[0].PushName != "Ally B" {
		t.Fatalf("contacts %+v %v", list, err)
	}
	var raw string
	db.SQL().QueryRow(`SELECT full_name FROM wa_contacts`).Scan(&raw)
	if !strings.HasPrefix(raw, "wae2:") {
		t.Fatalf("name stored in the clear: %q", raw)
	}
	pairs, _ := db.LIDPairs(ctx)
	if len(pairs) != 1 || pairs[0].PN != alice {
		t.Fatalf("lid map %+v", pairs)
	}
	if v, _ := db.Meta(ctx, "own_lid"); v != "200000000000001@lid" {
		t.Fatalf("own_lid %q", v)
	}
}

func TestReactionPruningUsesTheIndex(t *testing.T) {
	db := openTemp(t)
	rows, err := db.SQL().Query(`EXPLAIN QUERY PLAN SELECT rowid FROM reactions WHERE added_at < 5 ORDER BY added_at LIMIT 10`)
	if err != nil {
		t.Fatal(err)
	}
	var plan []string
	for rows.Next() {
		var id, parent, unused int
		var detail string
		rows.Scan(&id, &parent, &unused, &detail)
		plan = append(plan, detail)
	}
	rows.Close()
	if !strings.Contains(strings.Join(plan, " "), "reactions_added") {
		t.Fatalf("plan %v", plan)
	}
	// The count cap runs once per capEvery, not on every reaction.
	now := time.Unix(1_800_000_000, 0)
	db.Now = func() time.Time { return now }
	conv := "15550100002@s.whatsapp.net"
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "GHOST0", Reactor: conv, Emoji: "ok", At: 1})
	first := db.lastCapPrune
	now = now.Add(10 * time.Second)
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "GHOST1", Reactor: conv, Emoji: "ok", At: 1})
	if db.lastCapPrune != first {
		t.Fatal("the cap ran again within capEvery")
	}
	now = now.Add(capEvery)
	db.SetReaction(ctx, Reaction{Conv: conv, MsgID: "GHOST2", Reactor: conv, Emoji: "ok", At: 1})
	if db.lastCapPrune == first {
		t.Fatal("the cap did not run after capEvery")
	}
}

func TestDerivedTextIsBoundToItsSource(t *testing.T) {
	db := openTemp(t)
	conv := "15550100002@s.whatsapp.net"
	for _, src := range []string{"transcript", "image"} {
		db.SQL().Exec(`INSERT INTO derived_text VALUES (?, 'M1', ?, 'en', NULL, ?, 1)`, conv, src, "text of "+src)
	}
	if _, err := db.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey)); err != nil {
		t.Fatal(err)
	}
	for _, src := range []string{"transcript", "image"} {
		var v string
		db.SQL().QueryRow(`SELECT content FROM derived_text WHERE source = ?`, src).Scan(&v)
		got, err := db.seal.OpenAAD(v, crypt.AAD("derived_text", "content", conv, "M1", src))
		if err != nil || string(got) != "text of "+src {
			t.Fatalf("%s: %q %v", src, got, err)
		}
		if _, err := db.seal.OpenAAD(v, crypt.AAD("derived_text", "content", conv, "M1")); err == nil {
			t.Fatalf("%s opened without its source", src)
		}
	}
}
