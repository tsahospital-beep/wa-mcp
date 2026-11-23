package store

import (
	"context"
	"database/sql"
)

// States of an outbox_log row.
const (
	PartReserved = "reserved" // taken before the send; not known to be sent
	PartSent     = "sent"     // sent, msg_id is known
	PartUnknown  = "unknown"  // the send timed out; it may have been delivered
)

// SentPart is one part of an earlier send of the same request.
type SentPart struct {
	ID    int64
	Part  int
	MsgID string
	State string
	At    int64
	// Legacy is set for rows written before parts were recorded.
	Legacy bool
}

// ReserveParts writes one reserved outbox row per part before a send, so
// concurrent sends see them in their limit checks. Rows older than two days
// are pruned.
func (d *DB) ReserveParts(ctx context.Context, target, bodyHash string, at int64, parts []int) ([]int64, error) {
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	ids := make([]int64, 0, len(parts))
	for _, part := range parts {
		res, err := tx.ExecContext(ctx, `INSERT INTO outbox_log (target, body_hash, sent_at, part, state) VALUES (?, ?, ?, ?, ?)`,
			target, bodyHash, at, part, PartReserved)
		if err != nil {
			return nil, err
		}
		id, err := res.LastInsertId()
		if err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM outbox_log WHERE sent_at < ?`, at-2*86400); err != nil {
		return nil, err
	}
	return ids, tx.Commit()
}

// MarkPart records the outcome of one reserved part.
func (d *DB) MarkPart(ctx context.Context, id int64, state, msgID string) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE outbox_log SET state = ?, msg_id = ? WHERE id = ?`, state, nullText(msgID), id)
	return err
}

// SentParts returns the outbox rows of the same request to target since the
// given time, newest first.
func (d *DB) SentParts(ctx context.Context, target, bodyHash string, since int64) ([]SentPart, error) {
	rows, err := d.sql.QueryContext(ctx, `SELECT id, part, msg_id, state, sent_at FROM outbox_log
		WHERE target = ? AND body_hash = ? AND sent_at >= ? ORDER BY sent_at DESC, id DESC`, target, bodyHash, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SentPart
	for rows.Next() {
		var p SentPart
		var msgID sql.NullString
		if err := rows.Scan(&p.ID, &p.Part, &msgID, &p.State, &p.At); err != nil {
			return nil, err
		}
		p.MsgID = msgID.String
		p.Legacy = p.State == PartSent && !msgID.Valid
		out = append(out, p)
	}
	return out, rows.Err()
}
