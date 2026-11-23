package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
)

// Contact holds the names WhatsApp knows for one address (address book,
// push name, business name), copied from session.db into wa_contacts so that
// readers never need the session file. Names are sealed like message text.
type Contact struct {
	JID          string
	FirstName    string
	FullName     string
	PushName     string
	BusinessName string
}

// LIDPair maps a LID address to a phone-number address, both full JIDs
// ("200000000000002@lid", "15550100002@s.whatsapp.net").
type LIDPair struct {
	LID string
	PN  string
}

// PutContacts inserts or updates contacts in one transaction.
func (d *DB) PutContacts(ctx context.Context, list []Contact) error {
	if len(list) == 0 {
		return nil
	}
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := d.now().Unix()
	for _, c := range list {
		if c.JID == "" {
			continue
		}
		vals := []string{c.FirstName, c.FullName, c.PushName, c.BusinessName}
		for i, col := range []string{"first_name", "full_name", "push_name", "business_name"} {
			if vals[i], err = d.sealText(vals[i], field{"wa_contacts", col, c.JID, ""}); err != nil {
				return err
			}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO wa_contacts (jid, first_name, full_name, push_name, business_name, updated_at)
			VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (jid) DO UPDATE SET first_name = excluded.first_name,
			full_name = excluded.full_name, push_name = excluded.push_name, business_name = excluded.business_name,
			updated_at = excluded.updated_at`, c.JID, vals[0], vals[1], vals[2], vals[3], now); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// Contacts returns all copied contacts with their names opened.
func (d *DB) Contacts(ctx context.Context) ([]Contact, error) {
	rows, err := d.sql.QueryContext(ctx, `SELECT jid, first_name, full_name, push_name, business_name FROM wa_contacts ORDER BY jid`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Contact
	for rows.Next() {
		var c Contact
		if err := rows.Scan(&c.JID, &c.FirstName, &c.FullName, &c.PushName, &c.BusinessName); err != nil {
			return nil, err
		}
		c.FirstName = d.openText(c.FirstName, field{"wa_contacts", "first_name", c.JID, ""})
		c.FullName = d.openText(c.FullName, field{"wa_contacts", "full_name", c.JID, ""})
		c.PushName = d.openText(c.PushName, field{"wa_contacts", "push_name", c.JID, ""})
		c.BusinessName = d.openText(c.BusinessName, field{"wa_contacts", "business_name", c.JID, ""})
		out = append(out, c)
	}
	return out, rows.Err()
}

// PutLIDPairs inserts or updates LID mappings in one transaction.
func (d *DB) PutLIDPairs(ctx context.Context, list []LIDPair) error {
	if len(list) == 0 {
		return nil
	}
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := d.now().Unix()
	for _, p := range list {
		if p.LID == "" || p.PN == "" {
			continue
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO wa_lid_map (lid, pn, updated_at) VALUES (?, ?, ?)
			ON CONFLICT (lid) DO UPDATE SET pn = excluded.pn, updated_at = excluded.updated_at`, p.LID, p.PN, now); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// LIDPairs returns the copied LID map.
func (d *DB) LIDPairs(ctx context.Context) ([]LIDPair, error) {
	rows, err := d.sql.QueryContext(ctx, `SELECT lid, pn FROM wa_lid_map ORDER BY lid`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []LIDPair
	for rows.Next() {
		var p LIDPair
		if err := rows.Scan(&p.LID, &p.PN); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// SetOwnIDs records our own phone-number and LID addresses (meta own_jid, own_lid).
func (d *DB) SetOwnIDs(ctx context.Context, pn, lid string) error {
	if pn != "" {
		if err := d.SetMeta(ctx, "own_jid", pn); err != nil {
			return err
		}
	}
	if lid != "" {
		return d.SetMeta(ctx, "own_lid", lid)
	}
	return nil
}

// ReadSessionLIDMap reads the LID map of a whatsmeow session database
// read-only. whatsmeow keeps user parts only; the result has full JIDs.
func ReadSessionLIDMap(ctx context.Context, sessionPath string) ([]LIDPair, error) {
	sess, err := sql.Open("sqlite", readOnlyURI(sessionPath))
	if err != nil {
		return nil, err
	}
	defer sess.Close()
	rows, err := sess.QueryContext(ctx, `SELECT lid, pn FROM whatsmeow_lid_map`)
	if err != nil {
		if strings.Contains(err.Error(), "no such table") {
			return nil, nil
		}
		return nil, fmt.Errorf("session LID map: %w", err)
	}
	defer rows.Close()
	var out []LIDPair
	for rows.Next() {
		var lid, pn string
		if err := rows.Scan(&lid, &pn); err != nil {
			return nil, err
		}
		if lid == "" || pn == "" {
			continue
		}
		out = append(out, LIDPair{LID: lid + "@lid", PN: pn + "@s.whatsapp.net"})
	}
	if err := rows.Err(); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	return out, nil
}
