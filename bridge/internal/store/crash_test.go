package store

import (
	"bytes"
	"database/sql"
	"encoding/base64"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/crypt"
)

// A kill after the migration commit and before the compaction: the mark
// written with the commit makes the next start compact.
func TestCrashBeforeCompactionIsFinishedAtNextStart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wa.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	fillPlain(t, db)
	for i := 0; i < 400; i++ {
		if err := db.PutMessage(ctx, &Message{Conv: "15550100002@s.whatsapp.net", ID: fmt.Sprintf("X%d", i), SentAt: int64(1_800_000_100 + i), Body: marker + strings.Repeat(" pad", 40)}); err != nil {
			t.Fatal(err)
		}
	}
	salt, err := crypt.NewSalt()
	if err != nil {
		t.Fatal(err)
	}
	s, err := fieldSealer(KindKeystore, testStoreKey, salt)
	if err != nil {
		t.Fatal(err)
	}
	check, err := s.MakeCheck()
	if err != nil {
		t.Fatal(err)
	}
	err = db.withExclusive(ctx, func(c *sql.Conn) error {
		_, err := migrateTx(ctx, c, s, map[string]string{"crypt_kind": KindKeystore, "crypt_salt": base64.StdEncoding.EncodeToString(salt), "crypt_check": check})
		return err // process is killed here, before compact
	})
	if err != nil {
		t.Fatal(err)
	}
	if mark, err := db.Meta(ctx, metaCompactPending); err != nil || mark != "1" {
		t.Fatalf("after migration: compact_pending=%q, err=%v", mark, err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	db2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	st, err := db2.SetupKey(ctx, keyPlan(KindKeystore, testStoreKey))
	if err != nil || st.CompactPending {
		t.Fatalf("after restart: %+v, err=%v", st, err)
	}
	if err := db2.Close(); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(rawFiles(t, path), []byte(marker)) {
		t.Fatalf("plain text still on disk after the next start (CompactPending=%v)", st.CompactPending)
	}
}
