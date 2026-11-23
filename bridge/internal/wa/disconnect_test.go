package wa

import (
	"io"
	"log"
	"path/filepath"
	"testing"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestPermanentDisconnectIsRecordedAndCleared(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var emitted []map[string]any
	c, err := New(bg, db, Options{SessionPath: filepath.Join(dir, "session.db"), Log: log.New(io.Discard, "", 0), Terminal: io.Discard,
		Emit: func(e string, d any) {
			if m, ok := d.(map[string]any); ok && e == "connection" {
				emitted = append(emitted, m)
			}
		}})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	for _, evt := range []any{&events.ClientOutdated{}, &events.CATRefreshError{}, &events.ConnectFailure{Reason: events.ConnectFailureServiceUnavailable}} {
		c.handle(evt)
		if c.DisconnectReason() == "" {
			t.Fatalf("%T: no reason", evt)
		}
	}
	if len(emitted) != 3 || emitted[0]["state"] != "permanent_disconnect" || emitted[0]["reason"] != "client outdated" {
		t.Fatalf("webhook events %v", emitted)
	}
	c.handle(&events.TemporaryBan{Code: events.TempBanSentToTooManyPeople})
	if c.DisconnectReason() == "" || emitted[len(emitted)-1]["state"] != "temporary_ban" {
		t.Fatalf("temporary ban: %q %v", c.DisconnectReason(), emitted)
	}
	c.handle(&events.Connected{})
	if c.DisconnectReason() != "" {
		t.Fatal("the reason survived a new connection")
	}
}

func TestConcurrentRetryKeepsFirstWaiter(t *testing.T) {
	c := &Client{retryWait: map[retryKey]chan *events.MediaRetry{}}
	key := retryKey{bob, "A"}
	ch, release, err := c.waitForRetry(key)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.waitForRetry(key); err == nil {
		t.Fatal("replaced waiter")
	}
	other := retryKey{types.NewJID("15550100009", types.DefaultUserServer), "A"}
	_, releaseOther, err := c.waitForRetry(other)
	if err != nil {
		t.Fatal(err)
	}
	defer releaseOther()
	evt := &events.MediaRetry{ChatID: bob, MessageID: "A"}
	c.handle(evt)
	select {
	case got := <-ch:
		if got != evt {
			t.Fatal("wrong reply")
		}
	default:
		t.Fatal("lost reply")
	}
	release()
	_, release, err = c.waitForRetry(key)
	if err != nil {
		t.Fatal(err)
	}
	release()
}
