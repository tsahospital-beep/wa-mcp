package api

import (
	"context"
	"crypto/sha256"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"go.mau.fi/whatsmeow/types"
)

func TestAgentFeedOnlyLiveReferences(t *testing.T) {
	f := make(AgentFeed, 1)
	m := &store.Message{Conv: "chat", ID: "one", Bot: true, Body: "private words"}
	if !f.Offer(m) || f.Offer(m) {
		t.Fatal("expected one bounded slot")
	}
	s := &Server{Agent: f}
	w := httptest.NewRecorder()
	s.nextAgent(w, httptest.NewRequest("POST", "/agent/next", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"bot":true`) || strings.Contains(w.Body.String(), "private words") {
		t.Fatal(w.Body.String())
	}
	if !f.Offer(&store.Call{Peer: "caller", Group: "group", ID: "two", Outcome: store.CallMissed, EndedAt: 7}) {
		t.Fatal("call dropped")
	}
	e := <-f
	if e.Chat != "group" || e.At != 7 || e.Outcome != store.CallMissed {
		t.Fatal(e)
	}
}

func TestAgentRejectAndDecision(t *testing.T) {
	e := newEnv(t, true)
	cfg := e.cfg
	cfg.Agent.Enabled = true
	cfg.Agent.Mode = "auto"
	cfg.Agent.AutoReject = true
	cfg.File = filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(cfg.File, []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	revision := fmt.Sprintf("%x", sha256.Sum256([]byte("{}\x00")))
	peer := types.NewJID("123", types.DefaultUserServer)
	creator := types.NewJID("456", types.HiddenUserServer)
	if err := e.db.NoteCall(context.Background(), store.Call{ID: "ring", Peer: peer.String(), Creator: creator.String(), StartedAt: time.Now().Unix()}); err != nil {
		t.Fatal(err)
	}
	var events []string
	var reportedOutcome string
	count := 0
	entered, release := make(chan struct{}), make(chan struct{})
	s := &Server{Agent: make(AgentFeed, 1), Cfg: cfg, DB: e.db, Emit: func(name string, data any) {
		events = append(events, name)
		if call, ok := data.(*store.Call); ok {
			reportedOutcome = call.Outcome
		}
	}, RejectCall: func(_ context.Context, j types.JID, id string) error {
		if j != creator || id != "ring" {
			t.Fatal(j, id)
		}
		count++
		close(entered)
		<-release
		return nil
	}}
	body := fmt.Sprintf(`{"id":"ring","chat":%q,"revision":%q}`, peer.String(), revision)
	reject := func() int {
		w := httptest.NewRecorder()
		s.rejectAgentCall(w, httptest.NewRequest("POST", "/agent/reject", strings.NewReader(body)))
		return w.Code
	}
	if err := os.WriteFile(filepath.Join(cfg.StoreDir, "agent.switch"), []byte("paused 1"), 0600); err != nil {
		t.Fatal(err)
	}
	if reject() != 403 {
		t.Fatal("paused call rejected")
	}
	if err := os.Remove(filepath.Join(cfg.StoreDir, "agent.switch")); err != nil {
		t.Fatal(err)
	}
	first := make(chan int, 1)
	go func() { first <- reject() }()
	select {
	case <-entered:
	case status := <-first:
		t.Fatalf("rejection returned early: %d", status)
	}
	overlap := reject()
	if err := e.db.SetCallOutcome(context.Background(), "ring", store.CallAnswered); err != nil {
		t.Fatal(err)
	}
	close(release)
	if <-first != 200 || overlap != 409 || reject() != 409 || count != 1 || reportedOutcome != store.CallAnswered {
		t.Fatal("expected one rejection")
	}
	w := httptest.NewRecorder()
	s.agentReply(w, httptest.NewRequest("POST", "/agent/reply", strings.NewReader(`{"status":"drafted","chat":"test","message_id":"one","reason":"draft"}`)))
	if w.Code != 200 || strings.Join(events, ",") != "call,agent.reply" {
		t.Fatal(w.Code, events)
	}
}
