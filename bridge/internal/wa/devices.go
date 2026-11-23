package wa

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// whatsmeow has no event for changes of the account's own device list, so the
// list is compared from time to time: GetUserDevices answers from the cache
// that whatsmeow keeps current from device notifications, and asks the server
// only when the cache is empty.
const (
	deviceCheckDelay = 30 * time.Second
	DeviceCheckEvery = 6 * time.Hour
	metaOwnDevices   = "own_devices"
)

func (c *Client) securityEvent(kind, detail string) {
	if c.opts.Security != nil {
		c.opts.Security(kind, detail)
	}
}

// startDeviceWatch runs the device check after connecting and then every
// DeviceCheckEvery until Close; only one watch runs.
func (c *Client) startDeviceWatch() {
	if c.opts.Security == nil || !c.watching.CompareAndSwap(false, true) {
		return
	}
	go func() {
		defer c.watching.Store(false)
		wait := deviceCheckDelay
		for {
			select {
			case <-c.workCtx.Done():
				return
			case <-time.After(wait):
			}
			if !c.Connected() {
				return // the next Connected event starts a new watch
			}
			ctx, cancel := context.WithTimeout(c.workCtx, 30*time.Second)
			c.checkDevices(ctx)
			cancel()
			wait = DeviceCheckEvery
		}
	}()
}

func (c *Client) checkDevices(ctx context.Context) {
	wm, err := c.ready()
	if err != nil {
		return
	}
	session := c.session()
	if session.id.IsEmpty() {
		return
	}
	pn, lid := session.id.ToNonAD(), session.lid.ToNonAD()
	mine := session.id.Device
	list, err := wm.GetUserDevices(ctx, []types.JID{pn})
	if err != nil {
		c.opts.Log.Printf("device check: %v", err)
		return
	}
	c.compareDevices(ctx, ownDeviceIDs(list, pn, lid, mine, func(j types.JID) types.JID { return c.altOf(ctx, j) }))
}

// ownDeviceIDs keeps the device numbers of the account, without this bridge's
// own device. Devices can come addressed by phone number or by LID; a LID
// device counts when it is our own LID or the LID map points it to our number.
func ownDeviceIDs(list []types.JID, pn, lid types.JID, mine uint16, altOf func(types.JID) types.JID) []uint16 {
	var out []uint16
	for _, j := range list {
		ours := false
		switch j.Server {
		case types.DefaultUserServer:
			ours = j.User == pn.User
		case types.HiddenUserServer:
			ours = (!lid.IsEmpty() && j.User == lid.User) || (altOf != nil && altOf(j.ToNonAD()).User == pn.User)
		}
		if ours && j.Device != mine && !slices.Contains(out, j.Device) {
			out = append(out, j.Device)
		}
	}
	slices.Sort(out)
	return out
}

func diffDevices(old, now []uint16) (added, removed []uint16) {
	for _, d := range now {
		if !slices.Contains(old, d) {
			added = append(added, d)
		}
	}
	for _, d := range old {
		if !slices.Contains(now, d) {
			removed = append(removed, d)
		}
	}
	return added, removed
}

// compareDevices records added and removed devices against the list kept in
// meta.own_devices. The first list is stored silently as the baseline.
func (c *Client) compareDevices(ctx context.Context, now []uint16) {
	prevText, err := c.db.Meta(ctx, metaOwnDevices)
	if err != nil && !errors.Is(err, store.ErrNotFound) {
		return
	}
	if err == nil {
		var prev []uint16
		if json.Unmarshal([]byte(prevText), &prev) == nil {
			added, removed := diffDevices(prev, now)
			for _, d := range added {
				c.securityEvent(security.DeviceAdded, fmt.Sprintf("a new device (number %d) was linked to this account. "+
					"If it is not yours, remove it under Linked devices on your phone", d))
			}
			for _, d := range removed {
				c.securityEvent(security.DeviceRemoved, fmt.Sprintf("the linked device number %d was removed from this account", d))
			}
		}
	}
	if now == nil {
		now = []uint16{}
	}
	b, _ := json.Marshal(now)
	if err := c.db.SetMeta(ctx, metaOwnDevices, string(b)); err != nil {
		c.opts.Log.Printf("device check: %v", err)
	}
}
