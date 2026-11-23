package wa

import (
	"context"
	"errors"
	"io"
	"log"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waAdv"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

type recorded struct{ kind, detail string }

func TestIdentityRequiresMatchingServerOrMappedAlias(t *testing.T) {
	pn := types.NewJID("15550100001", types.DefaultUserServer)
	other := types.NewJID(pn.User, types.HiddenUserServer)
	alias := types.NewJID("200000000000001", types.HiddenUserServer)
	in := Ingest{Self: func() (types.JID, types.JID) { return pn, alias }}
	if in.isSelf(other) || in.sameUser(bg, pn.String(), other) || (groupInfo{Admins: []types.JID{pn}}).isAdmin(other) {
		t.Fatal("an unrelated LID matched a phone address")
	}
	in.AltOf = func(_ context.Context, j types.JID) types.JID {
		if j == pn {
			return alias
		}
		return types.EmptyJID
	}
	if !in.sameUser(bg, pn.String(), alias) || !in.isSelf(alias) {
		t.Fatal("known LID did not match")
	}
}

func newSecurityClient(t *testing.T) (*Client, *[]recorded) {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	var got []recorded
	c, err := New(bg, db, Options{SessionPath: filepath.Join(dir, "session.db"), Log: log.New(io.Discard, "", 0),
		Terminal: io.Discard, Security: func(kind, detail string) { got = append(got, recorded{kind, detail}) }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return c, &got
}

func TestWaitForDeviceRemoval(t *testing.T) {
	c, _ := newSecurityClient(t)
	if err := c.waitForDeviceRemoval(bg); err != nil {
		t.Fatal(err)
	}
	dev := c.container.NewDevice()
	jid := types.NewADJID("15550100001", 0, 7)
	dev.ID = &jid
	dev.PushName = "Example"
	dev.Account = &waAdv.ADVSignedDeviceIdentity{
		Details: []byte{1}, AccountSignature: make([]byte, 64),
		AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64),
	}
	if err := dev.Save(bg); err != nil {
		t.Fatal(err)
	}
	flow := &loginFlow{c: c, ui: terminalUI{}}
	if err := flow.linked(bg); err != nil || c.NeedsLogin() || c.OwnJID() != jid.ToNonAD() || c.PushName() != "Example" {
		t.Fatalf("linked session: %v", err)
	}
	ctx, cancel := context.WithTimeout(bg, 20*time.Millisecond)
	defer cancel()
	if err := c.waitForDeviceRemoval(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("session still present: %v", err)
	}
	c.noteLogout("test")
	if !c.NeedsLogin() || c.accepting.Load() {
		t.Fatal("logged out session still available")
	}
	if _, err := c.ready(); !errors.Is(err, ErrNotLoggedIn) {
		t.Fatal(err)
	}
	if err := dev.Delete(bg); err != nil {
		t.Fatal(err)
	}
	if err := c.waitForDeviceRemoval(bg); err != nil {
		t.Fatal(err)
	}
}

func TestSessionEventsAreRecorded(t *testing.T) {
	c, got := newSecurityClient(t)
	c.mu.Lock()
	c.stopping = true // keep relogin from starting a login flow
	c.mu.Unlock()
	c.handle(&events.StreamReplaced{})
	c.handle(&events.LoggedOut{OnConnect: true, Reason: events.ConnectFailureLoggedOut})
	c.handle(&events.PairSuccess{ID: types.NewADJID("15550100001", 0, 7)})
	kinds := []string{}
	for _, r := range *got {
		kinds = append(kinds, r.kind)
		if strings.Contains(r.detail, "15550100001") {
			t.Fatalf("full number in %q", r.detail)
		}
	}
	if strings.Join(kinds, ",") != security.SessionReplaced+","+security.LoggedOut+","+security.Paired {
		t.Fatalf("kinds %v", kinds)
	}
}

func TestAccountMismatchIsRecorded(t *testing.T) {
	c, got := newSecurityClient(t)
	c.db.SetAccountJID(bg, "15550100001@s.whatsapp.net")
	other := types.NewJID("15550100009", types.DefaultUserServer)
	c.mu.Lock()
	c.linked.id = other
	c.mu.Unlock()
	if err := c.claimAccount(bg); !errors.Is(err, ErrAccountMismatch) {
		t.Fatalf("mismatch: %v", err)
	}
	if len(*got) != 1 || (*got)[0].kind != security.AccountMismatch || strings.Contains((*got)[0].detail, "15550100009") {
		t.Fatalf("events %+v", *got)
	}
}

func TestDeviceListChanges(t *testing.T) {
	pn := types.NewJID("15550100001", types.DefaultUserServer)
	lid := types.NewJID("200000000000001", types.HiddenUserServer)
	lidDev := func(user string, dev uint16) types.JID {
		return types.JID{User: user, Server: types.HiddenUserServer, Device: dev}
	}
	mapped := func(j types.JID) types.JID {
		if j.User == "200000000000099" {
			return pn // an older LID of ours, known from the LID map
		}
		return types.JID{}
	}
	ids := ownDeviceIDs([]types.JID{
		types.NewADJID("15550100001", 0, 0), types.NewADJID("15550100001", 0, 7), types.NewADJID("15550100001", 0, 12),
		types.NewADJID("15550100002", 0, 3), lidDev("200000000000001", 12), lidDev("200000000000001", 15),
		lidDev("200000000000099", 16), lidDev("200000000000002", 4),
	}, pn, lid, 7, mapped)
	if len(ids) != 4 || ids[0] != 0 || ids[1] != 12 || ids[2] != 15 || ids[3] != 16 {
		t.Fatalf("own ids %v", ids)
	}
	c, got := newSecurityClient(t)
	c.compareDevices(bg, []uint16{0, 12})
	if len(*got) != 0 {
		t.Fatalf("the baseline raised events: %+v", *got)
	}
	c.compareDevices(bg, []uint16{0, 12})
	c.compareDevices(bg, []uint16{0, 20})
	if len(*got) != 2 || (*got)[0].kind != security.DeviceAdded || !strings.Contains((*got)[0].detail, "number 20") ||
		(*got)[1].kind != security.DeviceRemoved {
		t.Fatalf("changes %+v", *got)
	}
	added, removed := diffDevices([]uint16{1, 2}, []uint16{2, 3})
	if len(added) != 1 || added[0] != 3 || len(removed) != 1 || removed[0] != 1 {
		t.Fatalf("diff %v %v", added, removed)
	}
}
