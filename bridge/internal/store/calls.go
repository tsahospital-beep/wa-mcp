package store

import (
	"context"
	"database/sql"
	"errors"
)

// Call outcomes.
const (
	CallUnknown  = "unknown"
	CallMissed   = "missed"
	CallRejected = "rejected"
	CallAnswered = "answered"
)

// Call is one row of call_log.
type Call struct {
	Creator   string `json:"-"` // Original signalling address, which can differ from Peer.
	GroupCall bool   `json:"group_call,omitempty"`
	ID        string `json:"call_id"`
	Peer      string `json:"peer"`
	Group     string `json:"group_jid,omitempty"`
	Outgoing  bool   `json:"outgoing"`
	Video     bool   `json:"video"`
	StartedAt int64  `json:"started_at"`
	EndedAt   int64  `json:"ended_at,omitempty"`
	Outcome   string `json:"outcome"`
}

// NoteCall inserts a call the first time it is seen. Later sightings can only
// add information (video flag, group), never reset the outcome.
func (d *DB) NoteCall(ctx context.Context, c Call) error {
	if c.Outcome == "" {
		c.Outcome = CallUnknown
	}
	_, err := d.sql.ExecContext(ctx, `INSERT INTO call_log (call_id, peer, group_jid, outgoing, video, started_at, ended_at, outcome, creator, group_call)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (call_id) DO UPDATE SET
		  creator = CASE WHEN call_log.creator = '' THEN excluded.creator ELSE call_log.creator END,
          group_call = MAX(call_log.group_call, excluded.group_call),
          peer = CASE WHEN call_log.peer = '' THEN excluded.peer ELSE call_log.peer END,
		  video = MAX(call_log.video, excluded.video),
		  group_jid = COALESCE(call_log.group_jid, excluded.group_jid),
		  ended_at = COALESCE(call_log.ended_at, excluded.ended_at),
		  outcome = CASE WHEN excluded.outcome = 'answered'
		    OR (call_log.outcome IN ('unknown','missed') AND excluded.outcome <> 'unknown')
		    THEN excluded.outcome ELSE call_log.outcome END,
		  started_at = MIN(call_log.started_at, excluded.started_at)`,
		c.ID, c.Peer, nullText(c.Group), boolInt(c.Outgoing), boolInt(c.Video), c.StartedAt, nullInt(c.EndedAt), c.Outcome, c.Creator, boolInt(c.GroupCall || c.Group != ""))
	return err
}

// SetCallOutcome records answered or rejected. An answered call stays answered.
func (d *DB) SetCallOutcome(ctx context.Context, id, outcome string) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE call_log SET outcome = ? WHERE call_id = ? AND outcome <> ?`, outcome, id, CallAnswered)
	return err
}

// EndCall stores the end time; a call that was never answered or rejected becomes missed.
func (d *DB) EndCall(ctx context.Context, id string, at int64) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE call_log SET
		ended_at = COALESCE(ended_at, ?),
		outcome = CASE WHEN outcome = ? THEN ? ELSE outcome END
		WHERE call_id = ?`, at, CallUnknown, CallMissed, id)
	return err
}

const callColumns = `call_id, peer, group_jid, outgoing, video, started_at, ended_at, outcome, creator, group_call`

// GetCall returns one call or ErrNotFound.
func (d *DB) GetCall(ctx context.Context, id string) (*Call, error) {
	row := d.sql.QueryRowContext(ctx, `SELECT `+callColumns+`
		FROM call_log WHERE call_id = ?`, id)
	c, err := scanCall(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return c, err
}

// ListCalls returns calls started at or after since, newest first.
func (d *DB) ListCalls(ctx context.Context, since int64, limit int) ([]Call, error) {
	if limit <= 0 || limit > 1000 {
		limit = 50
	}
	rows, err := d.sql.QueryContext(ctx, `SELECT `+callColumns+`
		FROM call_log WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?`, since, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Call{}
	for rows.Next() {
		c, err := scanCall(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *c)
	}
	return out, rows.Err()
}

func scanCall(row scanner) (*Call, error) {
	var c Call
	var group sql.NullString
	var ended sql.NullInt64
	var out, video int
	if err := row.Scan(&c.ID, &c.Peer, &group, &out, &video, &c.StartedAt, &ended, &c.Outcome, &c.Creator, &c.GroupCall); err != nil {
		return nil, err
	}
	c.Group, c.EndedAt = group.String, ended.Int64
	c.Outgoing, c.Video = out != 0, video != 0
	return &c, nil
}
