package login

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"time"
)

// Errors that Options.OpenAccount returns (wrapped) so the page can explain them.
var (
	ErrLinked     = errors.New("already linked")
	ErrNotRunning = errors.New("the bridge does not answer")
)

// accountsJSON is the account list the page shows ({{ACCOUNTS}}). It is
// empty with one account, so the window looks as before.
func (w *Window) accountsJSON() string {
	data := struct {
		Current string   `json:"current"`
		Names   []string `json:"names"`
	}{Names: []string{}}
	if len(w.opts.Accounts) > 1 {
		data.Current, data.Names = w.opts.Account, w.opts.Accounts
	}
	b, _ := json.Marshal(data) // escapes <, > and & for the script element
	return string(b)
}

// handleAccount answers {"name": "work"} with a one-time link to that account's
// own login window. The link comes from that account's bridge through its local
// API; nothing else about the other account is read or shown.
func (w *Window) handleAccount(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Name string `json:"name"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(rw, r.Body, 1024)).Decode(&req); err != nil {
		writeJSON(rw, http.StatusBadRequest, map[string]any{"ok": false, "error": "bad request"})
		return
	}
	if len(w.opts.Accounts) < 2 || req.Name == w.opts.Account || !slices.Contains(w.opts.Accounts, req.Name) {
		writeJSON(rw, http.StatusNotFound, map[string]any{"ok": false, "error": "unknown_account"})
		return
	}
	if w.opts.OpenAccount == nil {
		writeJSON(rw, http.StatusServiceUnavailable, map[string]any{"ok": false, "error": "not available"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	link, err := w.opts.OpenAccount(ctx, req.Name)
	switch {
	case errors.Is(err, ErrLinked):
		writeJSON(rw, http.StatusConflict, map[string]any{"ok": false, "error": "linked"})
	case errors.Is(err, ErrNotRunning):
		writeJSON(rw, http.StatusServiceUnavailable, map[string]any{"ok": false, "error": "not_running"})
	case err != nil:
		writeJSON(rw, http.StatusBadGateway, map[string]any{"ok": false, "error": err.Error()})
	default:
		writeJSON(rw, http.StatusOK, map[string]any{"ok": true, "url": link})
	}
}
