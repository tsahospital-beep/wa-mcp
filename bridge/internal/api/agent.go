package api

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"go.mau.fi/whatsmeow/types"
)

// AgentEvent contains live routing metadata only. The worker reads content from the account store.
type AgentEvent struct {
	Kind      string `json:"kind"`
	Chat      string `json:"chat"`
	ID        string `json:"id"`
	At        int64  `json:"at"`
	Outcome   string `json:"outcome,omitempty"`
	GroupCall bool   `json:"group_call,omitempty"`
	Bot       bool   `json:"bot,omitempty"`
	Outgoing  bool   `json:"outgoing,omitempty"`
}

type AgentFeed chan AgentEvent

func (f AgentFeed) Offer(data any) bool {
	if f == nil {
		return true
	}
	var e AgentEvent
	switch v := data.(type) {
	case *store.Message:
		e = AgentEvent{Kind: "message", Chat: v.Conv, ID: v.ID, Bot: v.Bot, Outgoing: v.Outgoing, At: v.SentAt}
	case *store.Call:
		j, _ := types.ParseJID(v.Peer)
		e = AgentEvent{Kind: "call", Chat: v.Peer, ID: v.ID, Outcome: v.Outcome, GroupCall: v.GroupCall || v.Group != "", Bot: j.IsBot(), Outgoing: v.Outgoing, At: v.StartedAt}
		if v.EndedAt != 0 {
			e.At = v.EndedAt
		}
		if v.Group != "" {
			e.Chat = v.Group
		}
	default:
		return true
	}
	select {
	case f <- e:
		return true
	default:
		return false
	}
}

func (s *Server) nextAgent(w http.ResponseWriter, r *http.Request) {
	if s.Agent == nil {
		fail(w, 403, "agent is disabled")
		return
	}
	select {
	case e := <-s.Agent:
		reply(w, 200, map[string]any{"event": e})
	case <-time.After(3 * time.Second):
		reply(w, 200, map[string]any{"event": nil})
	case <-r.Context().Done():
	}
}

func (s *Server) agentReply(w http.ResponseWriter, r *http.Request) {
	var v struct {
		Status  string `json:"status"`
		ID      string `json:"draft_id"`
		Reason  string `json:"reason"`
		Chat    string `json:"chat"`
		Message string `json:"message_id"`
	}
	if !decode(w, r, &v) {
		return
	}
	switch v.Status {
	case "drafted", "sent", "skipped", "queued":
	default:
		fail(w, 400, "invalid decision")
		return
	}
	s.logf("agent decision: %s", v.Status)
	if s.Emit != nil {
		s.Emit("agent.reply", v)
	}
	reply(w, 200, nil)
}

func (s *Server) rejectAgentCall(w http.ResponseWriter, r *http.Request) {
	if s.Agent == nil || !s.Cfg.Agent.AutoReject || s.Cfg.Agent.Mode != "auto" || s.Cfg.Agent.DryRun || !s.Cfg.Sending.Enabled || s.RejectCall == nil {
		fail(w, 403, "automatic rejection is disabled")
		return
	}
	var req struct {
		ID       string `json:"id"`
		Chat     string `json:"chat"`
		Revision string `json:"revision"`
	}
	if !decode(w, r, &req) {
		return
	}
	if !s.Cfg.AgentCurrent(req.Revision) {
		fail(w, 403, "agent settings changed")
		return
	}
	if _, busy := s.rejecting.LoadOrStore(req.ID, struct{}{}); busy {
		fail(w, http.StatusConflict, "call rejection already in progress")
		return
	}
	defer s.rejecting.Delete(req.ID)
	call, err := s.DB.GetCall(r.Context(), req.ID)
	if err != nil && !errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusInternalServerError, "reading call state failed")
		return
	}
	now := s.now().Unix()
	if call == nil || call.Peer != req.Chat || call.Outgoing || call.GroupCall || call.Group != "" || call.Outcome != store.CallUnknown || call.EndedAt != 0 || call.StartedAt > now || call.StartedAt < now-30 {
		fail(w, 409, "no matching incoming call")
		return
	}
	peer, err := types.ParseJID(call.Creator)
	if err != nil || peer.User == "" || peer.IsBot() {
		fail(w, 400, "invalid caller")
		return
	}
	if err := s.RejectCall(r.Context(), peer, call.ID); err != nil {
		fail(w, 502, "call rejection failed")
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), 5*time.Second)
	defer cancel()
	if err := s.DB.SetCallOutcome(ctx, call.ID, store.CallRejected); err != nil {
		fail(w, 500, "recording call rejection failed")
		return
	}
	call, err = s.DB.GetCall(ctx, call.ID)
	if err != nil {
		fail(w, 500, "reading call outcome failed")
		return
	}
	if s.Emit != nil {
		s.Emit("call", call)
	}
	reply(w, 200, nil)
}
