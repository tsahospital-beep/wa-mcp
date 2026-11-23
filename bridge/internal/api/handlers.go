package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/schedule"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	reply(w, http.StatusOK, map[string]any{
		"connected":       s.WA.Connected(),
		"logged_in":       s.WA.LoggedIn(),
		"account":         s.WA.Account(),
		"version":         s.Version,
		"sending_enabled": s.Cfg.Sending.Enabled,
		"security_events": s.Security.Events(),
		"security_alert":  s.Security.Alert(),
		"protection":      s.Security.Protection(),
	})
}

func (s *Server) chats(w http.ResponseWriter, r *http.Request) {
	limit, err := intParam(r, "limit", 50, 500)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	offset, err := intParam(r, "offset", 0, 0)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	list, err := s.DB.ListConversations(r.Context(), r.URL.Query().Get("query"), limit, offset)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	reply(w, http.StatusOK, map[string]any{"chats": list})
}

func (s *Server) messages(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit, err := intParam(r, "limit", 50, 500)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	offset, err := intParam(r, "offset", 0, 0)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	after, err := parseTime(q.Get("after"), s.Location)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	before, err := parseTime(q.Get("before"), s.Location)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	chat := q.Get("chat")
	if chat != "" {
		j, err := wa.ParseChat(chat)
		if err != nil {
			fail(w, http.StatusBadRequest, "chat: "+err.Error())
			return
		}
		chat = j.String()
	}
	list, err := s.DB.ListMessages(r.Context(), store.MessageQuery{
		Conv: chat, After: unix(after), Before: unix(before), Query: q.Get("query"),
		Limit: limit, Offset: offset, Ascending: q.Get("order") == "asc",
		IncludeDeleted: q.Get("include_deleted") == "true" || q.Get("include_deleted") == "1",
	})
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	out, err := s.withReactions(r.Context(), list)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	reply(w, http.StatusOK, map[string]any{"messages": out})
}

// sendStatus maps pipeline errors to HTTP status codes.
func sendStatus(err error) int {
	var le *sender.LimitError
	var pe *sender.PathError
	var ue *common.UpstreamError
	switch {
	case errors.Is(err, sender.ErrDisabled), errors.Is(err, sender.ErrQuietHours):
		return http.StatusForbidden
	case errors.As(err, &le):
		return http.StatusTooManyRequests
	case errors.Is(err, sender.ErrDuplicate):
		return http.StatusConflict
	case errors.As(err, &pe):
		if pe.Forbidden() {
			return http.StatusForbidden
		}
		return http.StatusBadRequest
	case errors.Is(err, sender.ErrEmpty), errors.Is(err, sender.ErrNotOpus), errors.Is(err, wa.ErrBadTarget),
		errors.Is(err, sender.ErrTooLarge), errors.Is(err, sender.ErrStickerFormat), errors.Is(err, sender.ErrMediaAndSticker):
		return http.StatusBadRequest
	case errors.Is(err, wa.ErrNotConnected), errors.Is(err, wa.ErrNotLoggedIn):
		return http.StatusServiceUnavailable
	case errors.Is(err, context.DeadlineExceeded):
		return http.StatusGatewayTimeout
	case errors.As(err, &ue):
		return http.StatusBadGateway
	}
	return http.StatusInternalServerError
}

func (s *Server) send(w http.ResponseWriter, r *http.Request) {
	if !s.Cfg.Sending.Enabled {
		fail(w, http.StatusForbidden, sender.ErrDisabled.Error())
		return
	}
	var req sender.Request
	if !decode(w, r, &req) {
		return
	}
	if !s.WA.LoggedIn() {
		fail(w, http.StatusServiceUnavailable, wa.ErrNotLoggedIn.Error())
		return
	}
	if !s.WA.Connected() {
		fail(w, http.StatusServiceUnavailable, wa.ErrNotConnected.Error())
		return
	}
	req.Scheduled = false
	// A send is not cut off when the client goes away: it would leave a
	// multi-part message half sent. It is bounded instead.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), HandlerTimeout-30*time.Second)
	defer cancel()
	res, err := s.Send(ctx, req)
	if err != nil {
		status := sendStatus(err)
		if status >= 500 {
			s.logf("send to %s: %v", common.MaskJID(req.To), err)
		}
		fail(w, status, err.Error())
		return
	}
	if res.Queued {
		reply(w, http.StatusAccepted, map[string]any{"queued": true, "scheduled_id": res.ScheduledID})
		return
	}
	out := map[string]any{"id": res.ID, "sent_at": unix(res.SentAt), "waited_ms": res.Waited.Milliseconds()}
	if len(res.IDs) > 1 {
		out["ids"] = res.IDs
	}
	reply(w, http.StatusOK, out)
}

func (s *Server) read(w http.ResponseWriter, r *http.Request) {
	if !s.Cfg.ReadReceipts.AllowMarkRead {
		fail(w, http.StatusForbidden, "marking messages read is disabled (read_receipts.allow_mark_read=false)")
		return
	}
	var req struct {
		Chat   string   `json:"chat"`
		IDs    []string `json:"ids"`
		Author string   `json:"author"`
	}
	if !decode(w, r, &req) {
		return
	}
	chat, err := wa.ParseChat(req.Chat)
	if err != nil {
		fail(w, http.StatusBadRequest, "chat: "+err.Error())
		return
	}
	if len(req.IDs) == 0 || len(req.IDs) > 200 {
		fail(w, http.StatusBadRequest, "ids: give 1 to 200 message ids")
		return
	}
	// Group read receipts name the author of each message.
	groups := map[string][]string{}
	if chat.Server == types.GroupServer && req.Author == "" {
		for _, id := range req.IDs {
			m, err := s.DB.GetMessage(r.Context(), chat.String(), id)
			if err != nil {
				fail(w, http.StatusBadRequest, "author is required for message "+id+" (not in the database)")
				return
			}
			groups[m.Author] = append(groups[m.Author], id)
		}
	} else {
		groups[req.Author] = req.IDs
	}
	now := s.now()
	marked := 0
	for author, ids := range groups {
		var sender types.JID
		if author != "" {
			if sender, err = wa.ParseChat(author); err != nil {
				fail(w, http.StatusBadRequest, "author: "+err.Error())
				return
			}
		}
		if err := s.WA.MarkRead(r.Context(), ids, now, chat, sender); err != nil {
			fail(w, sendStatus(err), err.Error())
			return
		}
		_ = s.DB.MarkRead(r.Context(), chat.String(), ids, now.Unix())
		marked += len(ids)
	}
	reply(w, http.StatusOK, map[string]any{"marked": marked})
}

func (s *Server) history(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Chat     string `json:"chat"`
		Count    int    `json:"count"`
		BeforeID string `json:"before_id"`
	}
	if !decode(w, r, &req) {
		return
	}
	chat, err := wa.ParseChat(req.Chat)
	if err != nil {
		fail(w, http.StatusBadRequest, "chat: "+err.Error())
		return
	}
	if req.Count <= 0 {
		req.Count = 50
	}
	if req.Count > 100 {
		req.Count = 100
	}
	var anchor *store.Message
	if req.BeforeID != "" {
		anchor, err = s.DB.GetMessage(r.Context(), chat.String(), req.BeforeID)
	} else {
		anchor, err = s.DB.OldestMessage(r.Context(), chat.String())
	}
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, "no stored message to anchor the request; the chat needs at least one known message")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := s.WA.RequestHistory(r.Context(), anchor, req.Count); err != nil {
		fail(w, sendStatus(err), err.Error())
		return
	}
	reply(w, http.StatusOK, map[string]any{
		"requested": req.Count, "anchor_id": anchor.ID, "oldest_known": anchor.SentAt,
		"note": "older messages arrive in the background if the phone is online",
	})
}

func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Chat string `json:"chat"`
		ID   string `json:"id"`
	}
	if !decode(w, r, &req) {
		return
	}
	chat, err := wa.ParseChat(req.Chat)
	if err != nil || req.ID == "" {
		fail(w, http.StatusBadRequest, "chat and id are required")
		return
	}
	res, err := s.Media.Fetch(r.Context(), chat.String(), req.ID)
	switch {
	case err == nil:
		reply(w, http.StatusOK, map[string]any{"path": res.Path, "kind": res.Kind, "attachment_name": res.AttachmentName})
	case errors.Is(err, store.ErrNotFound):
		fail(w, http.StatusNotFound, "message not found")
	case errors.Is(err, media.ErrNotMedia):
		fail(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, media.ErrIncomplete):
		fail(w, http.StatusUnprocessableEntity, err.Error())
	default:
		fail(w, sendStatus(err), err.Error())
	}
}

func (s *Server) avatar(w http.ResponseWriter, r *http.Request) {
	jid, err := wa.ParseChat(r.URL.Query().Get("jid"))
	if err != nil {
		fail(w, http.StatusBadRequest, "jid: "+err.Error())
		return
	}
	path, err := s.Avatar(r.Context(), jid)
	switch {
	case err == nil:
		reply(w, http.StatusOK, map[string]any{"path": path})
	case errors.Is(err, media.ErrNoAvatar):
		fail(w, http.StatusNotFound, err.Error())
	default:
		fail(w, sendStatus(err), err.Error())
	}
}

func (s *Server) calls(w http.ResponseWriter, r *http.Request) {
	since, err := parseTime(r.URL.Query().Get("since"), s.Location)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	limit, err := intParam(r, "limit", 50, 500)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	list, err := s.DB.ListCalls(r.Context(), unix(since), limit)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	reply(w, http.StatusOK, map[string]any{"calls": list})
}

// replyRef accepts either a message id string or {"id": ...}.
type replyRef struct{ ID string }

func (r *replyRef) UnmarshalJSON(b []byte) error {
	var id string
	if err := json.Unmarshal(b, &id); err == nil {
		r.ID = id
		return nil
	}
	var obj struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(b, &obj); err != nil {
		return errors.New("reply_to must be a message id or an object with id")
	}
	r.ID = obj.ID
	return nil
}

func (s *Server) addSchedule(w http.ResponseWriter, r *http.Request) {
	if !s.Cfg.Sending.Enabled {
		fail(w, http.StatusForbidden, sender.ErrDisabled.Error())
		return
	}
	var req struct {
		To          string    `json:"to"`
		Text        string    `json:"text"`
		MediaPath   string    `json:"media_path"`
		AsVoice     bool      `json:"as_voice"`
		StickerPath string    `json:"sticker_path"`
		Human       *bool     `json:"human"`
		ReplyTo     *replyRef `json:"reply_to"`
		Mentions    []string  `json:"mentions"`
		At          string    `json:"at"`
		Recurrence  string    `json:"recurrence"`
	}
	if !decode(w, r, &req) {
		return
	}
	at, err := parseTime(req.At, s.Location)
	if err != nil {
		fail(w, http.StatusBadRequest, "at: "+err.Error())
		return
	}
	e := schedule.Entry{To: req.To, Text: req.Text, MediaPath: req.MediaPath, AsVoice: req.AsVoice, StickerPath: req.StickerPath,
		Human: req.Human, Mentions: req.Mentions, At: at, Recurrence: req.Recurrence}
	if req.ReplyTo != nil {
		e.ReplyToID = req.ReplyTo.ID
	}
	sc, err := schedule.Create(r.Context(), s.DB, e, s.now(), s.Location, s.Cfg.Sending.AllowedMediaDirs, s.Cfg.Sending.DeniedPaths)
	if err != nil {
		status := sendStatus(err)
		if status >= 500 {
			status = http.StatusBadRequest
		}
		fail(w, status, err.Error())
		return
	}
	reply(w, http.StatusCreated, map[string]any{"id": sc.ID, "next_run": sc.NextRun, "scheduled": sc})
}

func (s *Server) listSchedule(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	switch status {
	case "", store.SchedPending, store.SchedSending, store.SchedSent, store.SchedFailed, store.SchedCancelled:
	default:
		fail(w, http.StatusBadRequest, "status must be pending, sending, sent, failed or cancelled")
		return
	}
	list, err := s.DB.ListScheduled(r.Context(), status)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	reply(w, http.StatusOK, map[string]any{"scheduled": list})
}

func (s *Server) cancelSchedule(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id <= 0 {
		fail(w, http.StatusBadRequest, "id must be a positive integer")
		return
	}
	sc, err := s.DB.GetScheduled(r.Context(), id)
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, "no such scheduled message")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	ok, err := s.DB.CancelScheduled(r.Context(), id)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !ok {
		fail(w, http.StatusConflict, "only pending entries can be cancelled (status is "+sc.Status+")")
		return
	}
	reply(w, http.StatusOK, map[string]any{"cancelled": true, "id": id})
}

func (s *Server) openapi(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/yaml; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	w.Write(openAPISpec)
}

// messageOut is a stored message plus its reactions.
type messageOut struct {
	store.Message
	Reactions []store.Reaction `json:"reactions,omitempty"`
}

func (s *Server) withReactions(ctx context.Context, list []store.Message) ([]messageOut, error) {
	byConv := map[string][]string{}
	for _, m := range list {
		byConv[m.Conv] = append(byConv[m.Conv], m.ID)
	}
	found := map[string]map[string][]store.Reaction{}
	for conv, ids := range byConv {
		r, err := s.DB.ReactionsFor(ctx, conv, ids)
		if err != nil {
			return nil, err
		}
		found[conv] = r
	}
	out := make([]messageOut, len(list))
	for i, m := range list {
		out[i] = messageOut{Message: m, Reactions: found[m.Conv][m.ID]}
	}
	return out, nil
}

// loginURL issues a one-time link to the login window, for users whose
// terminal output is redirected.
func (s *Server) loginURL(w http.ResponseWriter, r *http.Request) {
	link, err := s.WA.LoginURL()
	if errors.Is(err, wa.ErrNoLogin) {
		fail(w, http.StatusConflict, err.Error())
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	reply(w, http.StatusOK, map[string]any{"url": link})
}
