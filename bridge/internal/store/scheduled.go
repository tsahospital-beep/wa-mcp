package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
)

// Schedule states.
const (
	SchedPending   = "pending"
	SchedSending   = "sending"
	SchedSent      = "sent"
	SchedFailed    = "failed"
	SchedCancelled = "cancelled"
)

// Scheduled is one row of scheduled_messages.
type Scheduled struct {
	SourceChat      string   `json:"source_chat,omitempty"`
	SourceMessageID string   `json:"source_message_id,omitempty"`
	QuietQueued     bool     `json:"-"`
	AgentRevision   string   `json:"-"`
	ID              int64    `json:"id"`
	Target          string   `json:"target"`
	Body            string   `json:"body"`
	MediaPath       string   `json:"media_path,omitempty"`
	Kind            string   `json:"kind,omitempty"` // Media kind or quiet-hours action.
	Human           *bool    `json:"human,omitempty"`
	ReplyToID       string   `json:"reply_to_id,omitempty"`
	Mentions        []string `json:"mentions,omitempty"`
	NextRun         int64    `json:"next_run"`
	Recurrence      string   `json:"recurrence"`
	Status          string   `json:"status"`
	Attempts        int      `json:"attempts"`
	LastError       string   `json:"last_error,omitempty"`
	CreatedAt       int64    `json:"created_at"`
	LastSentAt      int64    `json:"last_sent_at,omitempty"`
	AnchorDay       int      `json:"-"`
}

const schedColumns = `id, target, body, media_path, kind, human, reply_to_id, mentions, next_run, recurrence, status,
	attempts, last_error, created_at, last_sent_at, anchor_day, agent_revision, quiet_queued, source_chat, source_message_id`

// AddScheduled stores a new pending entry and returns its id.
func (d *DB) AddScheduled(ctx context.Context, s *Scheduled) (int64, error) {
	if s.Recurrence == "" {
		s.Recurrence = "once"
	}
	if s.Status == "" {
		s.Status = SchedPending
	}
	body, err := d.sealText(s.Body, field{"scheduled_messages", "body", s.Target, ""})
	if err != nil {
		return 0, err
	}
	var mentions, human any
	if len(s.Mentions) > 0 {
		b, _ := json.Marshal(s.Mentions)
		mentions = string(b)
	}
	if s.Human != nil {
		human = boolInt(*s.Human)
	}
	res, err := d.sql.ExecContext(ctx, `INSERT INTO scheduled_messages
		(target, body, media_path, kind, human, reply_to_id, mentions, next_run, recurrence, status, attempts, created_at, anchor_day, agent_revision, quiet_queued, source_chat, source_message_id)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)`,
		s.Target, body, nullText(s.MediaPath), nullText(s.Kind), human, nullText(s.ReplyToID), mentions, s.NextRun,
		s.Recurrence, s.Status, s.CreatedAt, nullInt(int64(s.AnchorDay)), s.AgentRevision, s.QuietQueued, s.SourceChat, s.SourceMessageID)
	if err != nil {
		return 0, err
	}
	s.ID, err = res.LastInsertId()
	return s.ID, err
}

func (d *DB) scanScheduled(row scanner) (*Scheduled, error) {
	var s Scheduled
	var media, kind, reply, mentions, lastErr sql.NullString
	var human, lastSent, anchor sql.NullInt64
	if err := row.Scan(&s.ID, &s.Target, &s.Body, &media, &kind, &human, &reply, &mentions, &s.NextRun, &s.Recurrence,
		&s.Status, &s.Attempts, &lastErr, &s.CreatedAt, &lastSent, &anchor, &s.AgentRevision, &s.QuietQueued, &s.SourceChat, &s.SourceMessageID); err != nil {
		return nil, err
	}
	s.Body = d.openText(s.Body, field{"scheduled_messages", "body", s.Target, ""})
	s.MediaPath, s.Kind, s.ReplyToID, s.LastError = media.String, kind.String, reply.String, lastErr.String
	s.LastSentAt, s.AnchorDay = lastSent.Int64, int(anchor.Int64)
	if human.Valid {
		h := human.Int64 != 0
		s.Human = &h
	}
	if mentions.String != "" {
		_ = json.Unmarshal([]byte(mentions.String), &s.Mentions)
	}
	return &s, nil
}

func (d *DB) querySchedules(ctx context.Context, stmt string, args ...any) ([]Scheduled, error) {
	rows, err := d.sql.QueryContext(ctx, stmt, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Scheduled{}
	for rows.Next() {
		s, err := d.scanScheduled(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *s)
	}
	return out, rows.Err()
}

// GetScheduled returns one entry or ErrNotFound.
func (d *DB) GetScheduled(ctx context.Context, id int64) (*Scheduled, error) {
	s, err := d.scanScheduled(d.sql.QueryRowContext(ctx, `SELECT `+schedColumns+` FROM scheduled_messages WHERE id = ?`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return s, err
}

// DueScheduled returns pending entries with next_run <= now, oldest first.
func (d *DB) DueScheduled(ctx context.Context, now int64, limit int) ([]Scheduled, error) {
	if limit <= 0 {
		limit = 50
	}
	return d.querySchedules(ctx, `SELECT `+schedColumns+` FROM scheduled_messages
		WHERE status = ? AND next_run <= ? ORDER BY next_run ASC, id ASC LIMIT ?`, SchedPending, now, limit)
}

// ListScheduled returns entries with the given status (all when empty), soonest first.
func (d *DB) ListScheduled(ctx context.Context, status string) ([]Scheduled, error) {
	if status == "" {
		return d.querySchedules(ctx, `SELECT `+schedColumns+` FROM scheduled_messages ORDER BY next_run ASC, id ASC`)
	}
	return d.querySchedules(ctx, `SELECT `+schedColumns+` FROM scheduled_messages WHERE status = ?
		ORDER BY next_run ASC, id ASC`, status)
}

// ClaimScheduled moves a pending entry to "sending"; false if someone else got it first.
func (d *DB) ClaimScheduled(ctx context.Context, id int64) (bool, error) {
	res, err := d.sql.ExecContext(ctx, `UPDATE scheduled_messages SET status = ? WHERE id = ? AND status = ?`,
		SchedSending, id, SchedPending)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}

// ScheduleUpdate is the result of one run attempt.
type ScheduleUpdate struct {
	Status     string
	NextRun    int64
	Attempts   int
	LastError  string
	LastSentAt int64 // 0 keeps the old value
}

// UpdateScheduled stores the outcome of a run.
func (d *DB) UpdateScheduled(ctx context.Context, id int64, u ScheduleUpdate) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE scheduled_messages SET
		status = ?, next_run = ?, attempts = ?, last_error = ?, last_sent_at = COALESCE(?, last_sent_at)
		WHERE id = ?`, u.Status, u.NextRun, u.Attempts, nullText(u.LastError), nullInt(u.LastSentAt), id)
	return err
}

// CancelScheduled cancels a pending entry; false if it was not pending.
func (d *DB) CancelScheduled(ctx context.Context, id int64) (bool, error) {
	res, err := d.sql.ExecContext(ctx, `UPDATE scheduled_messages SET status = ? WHERE id = ? AND status = ?`,
		SchedCancelled, id, SchedPending)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}
