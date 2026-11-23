package wa

import (
	"context"
	"errors"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// The bridge copies names (address book, push and business names), the LID
// map and its own addresses from session.db into wa.db (wa_contacts,
// wa_lid_map, meta own_jid and own_lid), so that readers such as the MCP
// server never need the session file. It copies everything when the account
// is known and every DirectoryEvery, and single contacts on name events.
// Only changed rows are written.
const DirectoryEvery = 10 * time.Minute

type dirCache struct {
	lidWarned bool
	mu        sync.Mutex
	loaded    bool
	contacts  map[string]store.Contact
	lids      map[string]string
}

func (c *Client) kickDirectory() {
	select {
	case c.dirKick <- struct{}{}:
	default:
	}
}

func (c *Client) runDirectory(ctx context.Context) {
	tick := time.NewTicker(DirectoryEvery)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-c.dirKick:
		case <-tick.C:
		}
		sctx, cancel := context.WithTimeout(ctx, time.Minute)
		if err := c.syncDirectory(sctx); err != nil {
			c.opts.Log.Printf("directory copy: %v", err)
		}
		cancel()
	}
}

// load fills the cache from wa.db once, so a restart does not rewrite every row.
func (c *Client) dirLoad(ctx context.Context) error {
	if c.dir.loaded {
		return nil
	}
	c.dir.contacts, c.dir.lids = map[string]store.Contact{}, map[string]string{}
	list, err := c.db.Contacts(ctx)
	if err != nil {
		return err
	}
	for _, ct := range list {
		c.dir.contacts[ct.JID] = ct
	}
	pairs, err := c.db.LIDPairs(ctx)
	if err != nil {
		return err
	}
	for _, p := range pairs {
		c.dir.lids[p.LID] = p.PN
	}
	c.dir.loaded = true
	return nil
}

// syncDirectory copies all names, the LID map and our own addresses.
func (c *Client) syncDirectory(ctx context.Context) error {
	if !c.accepting.Load() {
		return nil // before the account check nothing is copied
	}
	dev := c.session()
	if dev.id.IsEmpty() || dev.contacts == nil {
		return nil // not linked, or the device store is not initialized yet
	}
	pn, lid := dev.id.ToNonAD(), dev.lid.ToNonAD()
	own := ""
	if !lid.IsEmpty() {
		own = lid.String()
	}
	if err := c.db.SetOwnIDs(ctx, pn.String(), own); err != nil {
		return err
	}
	all, err := dev.contacts.GetAllContacts(ctx)
	if err != nil {
		return err
	}
	chats, err := c.db.UntitledDirectChats(ctx)
	if err != nil {
		return err
	}
	for _, id := range chats {
		jid, err := types.ParseJID(id)
		if err != nil {
			continue
		}
		name := contactTitle(all[jid])
		if name == "" {
			name = contactTitle(all[c.altOf(ctx, jid)])
		}
		if name != "" {
			if _, err := c.db.SetTitle(ctx, id, name, false); err != nil {
				return err
			}
		}
	}
	c.dir.mu.Lock()
	defer c.dir.mu.Unlock()
	if err := c.dirLoad(ctx); err != nil {
		return err
	}
	var changed []store.Contact
	for jid, info := range all {
		if ct, ok := c.changedContact(jid, info); ok {
			changed = append(changed, ct)
		}
	}
	if err := c.db.PutContacts(ctx, changed); err != nil {
		return err
	}
	for _, ct := range changed {
		c.dir.contacts[ct.JID] = ct
	}
	pairs, err := store.ReadSessionLIDMap(ctx, c.opts.SessionPath)
	if errors.Is(err, store.ErrNoLIDMap) {
		if !c.dir.lidWarned {
			c.dir.lidWarned = true
			c.opts.Log.Printf("warning: directory copy: %v; LID chats are not matched to phone numbers for the MCP server", err)
		}
		return nil
	}
	if err != nil {
		return err
	}
	var newPairs []store.LIDPair
	for _, p := range pairs {
		if c.dir.lids[p.LID] != p.PN {
			newPairs = append(newPairs, p)
		}
	}
	if err := c.db.PutLIDPairs(ctx, newPairs); err != nil {
		return err
	}
	for _, p := range newPairs {
		c.dir.lids[p.LID] = p.PN
	}
	return nil
}

func (c *Client) changedContact(jid types.JID, info types.ContactInfo) (store.Contact, bool) {
	ct := store.Contact{JID: jid.ToNonAD().String(), FirstName: info.FirstName, FullName: info.FullName,
		PushName: info.PushName, BusinessName: info.BusinessName}
	if ct.FirstName == "" && ct.FullName == "" && ct.PushName == "" && ct.BusinessName == "" {
		return ct, false
	}
	return ct, c.dir.contacts[ct.JID] != ct
}

// syncContacts copies the current names of a few addresses after a name event.
func (c *Client) syncContacts(ctx context.Context, jids ...types.JID) {
	dev := c.session()
	if dev.id.IsEmpty() || dev.contacts == nil || !c.accepting.Load() {
		return
	}
	c.dir.mu.Lock()
	defer c.dir.mu.Unlock()
	if err := c.dirLoad(ctx); err != nil {
		return
	}
	var changed []store.Contact
	for _, j := range jids {
		if j.IsEmpty() {
			continue
		}
		info, err := dev.contacts.GetContact(ctx, j.ToNonAD())
		if err != nil || !info.Found {
			continue
		}
		if ct, ok := c.changedContact(j, info); ok {
			changed = append(changed, ct)
		}
	}
	if err := c.db.PutContacts(ctx, changed); err != nil {
		c.opts.Log.Printf("directory copy: %v", err)
		return
	}
	for _, ct := range changed {
		c.dir.contacts[ct.JID] = ct
	}
}
