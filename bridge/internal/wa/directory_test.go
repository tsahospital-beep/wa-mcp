package wa

import (
	"database/sql"
	"log"
	"path/filepath"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waAdv"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func TestDirectoryIsCopiedIntoWaDB(t *testing.T) {
	c, _ := newSecurityClient(t)
	dev := c.cur().Store
	own := types.NewADJID("15550100001", 0, 7)
	ownLID := types.NewADJID("200000000000001", 0, 7)
	ownLID.Server = types.HiddenUserServer
	dev.ID, dev.LID = &own, ownLID
	dev.Account = &waAdv.ADVSignedDeviceIdentity{Details: []byte{1}, AccountSignature: make([]byte, 64), AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64)}
	if err := c.container.PutDevice(bg, dev); err != nil {
		t.Fatal(err)
	}
	c.mu.Lock()
	c.linked = viewOf(dev)
	c.mu.Unlock()
	alice := types.NewJID("15550100002", types.DefaultUserServer)
	aliceLID := types.NewJID("200000000000002", types.HiddenUserServer)
	dev.Contacts.PutContactName(bg, alice, "Alice Example", "Alice")
	dev.Contacts.PutPushName(bg, alice, "Ally")
	dev.LIDs.PutLIDMapping(bg, aliceLID, alice)

	if err := c.db.TouchConversation(bg, store.Conversation{JID: aliceLID.String()}); err != nil {
		t.Fatal(err)
	}

	// Before the account check nothing is copied.
	if err := c.syncDirectory(bg); err != nil {
		t.Fatal(err)
	}
	if list, _ := c.db.Contacts(bg); len(list) != 0 {
		t.Fatalf("copied before the account check: %+v", list)
	}
	c.accepting.Store(true) // Run the directory copy synchronously in this test.
	if err := c.syncDirectory(bg); err != nil {
		t.Fatal(err)
	}
	list, _ := c.db.Contacts(bg)
	info, _ := dev.Contacts.GetContact(bg, alice)
	if chat, err := c.db.GetConversation(bg, aliceLID.String()); err != nil || chat.Title != info.FullName {
		t.Fatal(chat, err)
	}
	if len(list) != 1 || list[0].JID != alice.String() || list[0].FullName != info.FullName ||
		list[0].FirstName != info.FirstName || list[0].PushName != "Ally" || info.FullName == "" {
		t.Fatalf("contacts %+v, session %+v", list, info)
	}
	pairs, _ := c.db.LIDPairs(bg)
	if len(pairs) != 1 || pairs[0] != (store.LIDPair{LID: aliceLID.String(), PN: alice.String()}) {
		t.Fatalf("lid map %+v", pairs)
	}
	if v, _ := c.db.Meta(bg, "own_jid"); v != "15550100001@s.whatsapp.net" {
		t.Fatalf("own_jid %q", v)
	}
	if v, _ := c.db.Meta(bg, "own_lid"); !strings.HasPrefix(v, "200000000000001@lid") {
		t.Fatalf("own_lid %q", v)
	}

	// A push name event copies the new name at once.
	dev.Contacts.PutPushName(bg, alice, "Ally Two")
	c.handle(&events.PushName{JID: alice, NewPushName: "Ally Two"})
	if list, _ := c.db.Contacts(bg); len(list) != 1 || list[0].PushName != "Ally Two" {
		t.Fatalf("after the event: %+v", list)
	}
	// An unchanged directory writes nothing.
	var before int64
	c.db.SQL().QueryRow(`SELECT updated_at FROM wa_contacts`).Scan(&before)
	c.db.SQL().Exec(`UPDATE wa_contacts SET updated_at = 1`)
	c.syncDirectory(bg)
	var after int64
	c.db.SQL().QueryRow(`SELECT updated_at FROM wa_contacts`).Scan(&after)
	if after != 1 || before == 1 {
		t.Fatalf("an unchanged contact was written again (%d, %d)", before, after)
	}
}

func TestMissingLIDMapIsReportedOnce(t *testing.T) {
	c, _ := newSecurityClient(t)
	var buf strings.Builder
	c.opts.Log = log.New(&buf, "", 0)
	dev := c.cur().Store
	own := types.NewADJID("15550100001", 0, 7)
	dev.ID = &own
	dev.Account = &waAdv.ADVSignedDeviceIdentity{Details: []byte{1}, AccountSignature: make([]byte, 64),
		AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64)}
	if err := c.container.PutDevice(bg, dev); err != nil {
		t.Fatal(err)
	}
	c.mu.Lock()
	c.linked = viewOf(dev)
	c.mu.Unlock()
	c.accepting.Store(true) // Run the directory copy synchronously in this test.
	// The real sqlstore schema has the table.
	if _, err := store.ReadSessionLIDMap(bg, c.opts.SessionPath); err != nil {
		t.Fatalf("real session schema: %v", err)
	}
	// A session database without it (another whatsmeow layout).
	other := filepath.Join(t.TempDir(), "session.db")
	raw, _ := sql.Open("sqlite", other)
	raw.Exec(`CREATE TABLE whatsmeow_device (jid TEXT)`)
	raw.Close()
	c.opts.SessionPath = other
	for i := 0; i < 2; i++ {
		if err := c.syncDirectory(bg); err != nil {
			t.Fatal(err)
		}
	}
	if n := strings.Count(buf.String(), "whatsmeow_lid_map"); n != 1 {
		t.Fatalf("warned %d times: %s", n, buf.String())
	}
}
