// Package security records events that can mean that someone else uses, or
// tries to use, the WhatsApp session of a store: a remote logout, another
// client with the same session keys, a new device on the account, a different
// account at login. The last MaxEvents events are kept in wa.db
// (meta.security_events), returned by /api/v1/status and sent to webhooks as
// event "security". Details never contain secrets, full phone numbers or
// message text.
package security

import (
	"context"
	"encoding/json"
	"log"
	"maps"
	"sync"
	"time"
)

// MaxEvents is how many events are kept.
const MaxEvents = 20

// AlertWindow is how long a warning event keeps security_alert set.
const AlertWindow = 7 * 24 * time.Hour

// Event kinds.
const (
	LoggedOut       = "logged_out"
	SessionReplaced = "session_replaced"
	DeviceAdded     = "device_added"
	DeviceRemoved   = "device_removed"
	AccountMismatch = "account_mismatch"
	Paired          = "paired"
)

// warnings are the kinds that set security_alert.
var warnings = map[string]bool{LoggedOut: true, SessionReplaced: true, DeviceAdded: true, AccountMismatch: true}

// Event is one recorded event.
type Event struct {
	At     int64  `json:"at"`
	Kind   string `json:"kind"`
	Level  string `json:"level"` // warning or info
	Detail string `json:"detail"`
}

// MetaStore is the part of store.DB the monitor needs.
type MetaStore interface {
	Meta(ctx context.Context, key string) (string, error)
	SetMeta(ctx context.Context, key, value string) error
}

const metaKey = "security_events"

// Monitor records events. The zero value works without persistence.
type Monitor struct {
	Store MetaStore
	Log   *log.Logger
	Emit  func(event string, data any)
	Now   func() time.Time

	mu         sync.Mutex
	events     []Event // oldest first
	loaded     bool
	protection map[string]string
}

func (m *Monitor) now() time.Time {
	if m.Now != nil {
		return m.Now()
	}
	return time.Now()
}

func (m *Monitor) loadLocked() {
	if m.loaded {
		return
	}
	m.loaded = true
	if m.Store == nil {
		return
	}
	if v, err := m.Store.Meta(context.Background(), metaKey); err == nil {
		var list []Event
		if json.Unmarshal([]byte(v), &list) == nil {
			m.events = list
		}
	}
}

// Record keeps an event, logs it and sends it to webhooks.
func (m *Monitor) Record(kind, detail string) {
	if m == nil {
		return
	}
	ev := Event{At: m.now().Unix(), Kind: kind, Level: "info", Detail: detail}
	if warnings[kind] {
		ev.Level = "warning"
	}
	m.mu.Lock()
	m.loadLocked()
	m.events = append(m.events, ev)
	if len(m.events) > MaxEvents {
		m.events = m.events[len(m.events)-MaxEvents:]
	}
	data, _ := json.Marshal(m.events)
	m.mu.Unlock()
	if m.Store != nil {
		if err := m.Store.SetMeta(context.Background(), metaKey, string(data)); err != nil && m.Log != nil {
			m.Log.Printf("security: could not save the event: %v", err)
		}
	}
	if m.Log != nil {
		prefix := "security"
		if ev.Level == "warning" {
			prefix = "warning: security"
		}
		m.Log.Printf("%s: %s: %s", prefix, kind, detail)
	}
	if m.Emit != nil {
		m.Emit("security", ev)
	}
}

// Events returns the kept events, newest first.
func (m *Monitor) Events() []Event {
	if m == nil {
		return []Event{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.loadLocked()
	out := make([]Event, 0, len(m.events))
	for i := len(m.events) - 1; i >= 0; i-- {
		out = append(out, m.events[i])
	}
	return out
}

// Alert reports whether a warning event happened within AlertWindow.
func (m *Monitor) Alert() bool {
	if m == nil {
		return false
	}
	cutoff := m.now().Add(-AlertWindow).Unix()
	for _, e := range m.Events() {
		if e.Level == "warning" && e.At >= cutoff {
			return true
		}
	}
	return false
}

// SetProtection stores the protection summary shown in the status (no secrets).
func (m *Monitor) SetProtection(p map[string]string) {
	m.mu.Lock()
	m.protection = maps.Clone(p)
	m.mu.Unlock()
}

// Protection returns the protection summary.
func (m *Monitor) Protection() map[string]string {
	if m == nil {
		return map[string]string{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.protection == nil {
		return map[string]string{}
	}
	return maps.Clone(m.protection)
}
