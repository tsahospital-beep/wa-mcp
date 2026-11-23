package store

import "context"

// ReserveSent writes n outbox rows before a send, so concurrent sends see
// them in their limit checks. Rows older than two days are pruned.
func (d *DB) ReserveSent(ctx context.Context, target, bodyHash string, at int64, n int) ([]int64, error) {
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	ids := make([]int64, 0, n)
	for i := 0; i < n; i++ {
		res, err := tx.ExecContext(ctx, `INSERT INTO outbox_log (target, body_hash, sent_at) VALUES (?, ?, ?)`,
			target, bodyHash, at)
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

// ReleaseSent removes reservations for messages that were not sent.
func (d *DB) ReleaseSent(ctx context.Context, ids []int64) error {
	if len(ids) == 0 {
		return nil
	}
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	_, err := d.sql.ExecContext(ctx, `DELETE FROM outbox_log WHERE id IN (`+placeholders(len(ids))+`)`, args...)
	return err
}

// OtherRecipients counts distinct other chats with a sent or reserved copy of the same request.
func (d *DB) OtherRecipients(ctx context.Context, target, hash string, since int64) (int, error) {
	var count int
	err := d.sql.QueryRowContext(ctx,
		`SELECT COUNT(DISTINCT target) FROM outbox_log WHERE body_hash = ? AND sent_at >= ? AND target <> ?`,
		hash, since, target).Scan(&count)
	return count, err
}

// CountSent counts messages sent since the given time; target "" counts all chats.
func (d *DB) CountSent(ctx context.Context, target string, since int64) (int, error) {
	var n int
	var err error
	if target == "" {
		err = d.sql.QueryRowContext(ctx, `SELECT COUNT(*) FROM outbox_log WHERE sent_at >= ?`, since).Scan(&n)
	} else {
		err = d.sql.QueryRowContext(ctx, `SELECT COUNT(*) FROM outbox_log WHERE target = ? AND sent_at >= ?`,
			target, since).Scan(&n)
	}
	return n, err
}

// SentRecently reports whether the same body went to target since the given time.
func (d *DB) SentRecently(ctx context.Context, target, bodyHash string, since int64) (bool, error) {
	var n int
	err := d.sql.QueryRowContext(ctx, `SELECT COUNT(*) FROM outbox_log WHERE target = ? AND body_hash = ? AND sent_at >= ?`,
		target, bodyHash, since).Scan(&n)
	return n > 0, err
}
