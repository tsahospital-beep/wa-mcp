package api

import (
	"net/http"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	waStore "go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

func actionEnv(t *testing.T) (*env, string, string) {
	t.Helper()
	e := newEnv(t, true)
	own := e.tr.OwnJID()
	wm := &whatsmeow.Client{Store: &waStore.Device{ID: &own}}
	e.snd.BuildAction = func(a wa.MessageAction, chat, author types.JID) (*waE2E.Message, error) {
		return a.Build(wm, chat, author)
	}
	chat := "120363000000001@g.us"
	peer := "15550100002@s.whatsapp.net"
	for _, m := range []store.Message{
		{Conv: chat, ID: "IN", Author: peer, Body: "agenda", Kind: store.KindText},
		{Conv: chat, ID: "OWN", Author: own.String(), Outgoing: true, Body: "old", Kind: store.KindText, SentAt: time.Now().Unix()},
		{Conv: chat, ID: "PIC", Author: peer, Kind: store.KindImage},
	} {
		if err := e.db.PutMessage(t.Context(), &m); err != nil {
			t.Fatal(err)
		}
	}
	return e, chat, peer
}

func TestMessageActions(t *testing.T) {
	e, chat, peer := actionEnv(t)
	call := func(kind, id string, extra map[string]string, status int) {
		t.Helper()
		b := map[string]string{"chat": chat, "message_id": id}
		for k, v := range extra {
			b[k] = v
		}
		code, out := e.do(t, "POST", "/api/v1/messages/"+kind, b)
		if code != status {
			t.Fatalf("%s %s: %d %v", kind, id, code, out)
		}
	}
	call("react", "IN", map[string]string{"emoji": "\U0001f44d"}, 200)
	reaction := e.tr.sent[0].GetReactionMessage()
	if reaction.GetKey().GetFromMe() || reaction.GetKey().GetParticipant() != peer || reaction.GetKey().GetID() != "IN" || reaction.GetKey().GetRemoteJID() != chat {
		t.Fatal(reaction)
	}
	call("react", "IN", map[string]string{"emoji": "\U0001f44d"}, 409)
	call("react", "IN", nil, 400)
	call("react", "IN", map[string]string{"emoji": ""}, 200)
	call("forward", "IN", map[string]string{"to": peer, "expected_text": "stale"}, 409)
	call("forward", "PIC", map[string]string{"to": peer}, 400)
	call("forward", "IN", map[string]string{"to": peer}, 200)
	forwarded := e.tr.sent[2].GetExtendedTextMessage()
	if forwarded.GetText() != "agenda" || !forwarded.GetContextInfo().GetIsForwarded() {
		t.Fatal(forwarded)
	}
	if m, err := e.db.GetMessage(t.Context(), peer, "OUT1"); err != nil || m.Body != "agenda" {
		t.Fatal(m, err)
	}
	call("edit", "IN", map[string]string{"text": "new"}, 403)
	call("delete", "IN", nil, 403)
	call("edit", "OWN", map[string]string{"text": "new"}, 200)
	edit := e.tr.sent[3].GetEditedMessage().GetMessage().GetProtocolMessage()
	if edit.GetEditedMessage().GetConversation() != "new" || !edit.GetKey().GetFromMe() || edit.GetKey().GetID() != "OWN" {
		t.Fatal(edit)
	}
	if m, err := e.db.GetMessage(t.Context(), chat, "OWN"); err != nil || m.Body != "new" {
		t.Fatal(m, err)
	}
	call("delete", "OWN", nil, 200)
	revoke := e.tr.sent[4].GetProtocolMessage()
	if revoke.GetType() != waE2E.ProtocolMessage_REVOKE || !revoke.GetKey().GetFromMe() || revoke.GetKey().GetID() != "OWN" {
		t.Fatal(revoke)
	}
	if m, err := e.db.GetMessage(t.Context(), chat, "OWN"); err != nil || m.DeletedAt == 0 {
		t.Fatal(m, err)
	}
	call("react", "OWN", map[string]string{"emoji": ""}, 400)
	call("delete", "imp-fake", nil, 400)
	call("delete", "unknown", nil, 404)
	if len(e.tr.sent) != 5 {
		t.Fatal(len(e.tr.sent))
	}
}

func TestActionPolicy(t *testing.T) {
	off := newEnv(t, false)
	for _, kind := range []string{"react", "edit", "delete", "forward"} {
		if code, _ := off.do(t, "POST", "/api/v1/messages/"+kind, nil); code != http.StatusForbidden {
			t.Fatal(kind, code)
		}
	}
	e, chat, peer := actionEnv(t)
	q := &e.snd.Cfg.QuietHours
	q.Enabled = true
	q.Start = time.Now().Add(-time.Hour).Format("15:04")
	q.End = time.Now().Add(time.Hour).Format("15:04")
	q.Action = "queue"
	body := map[string]string{"chat": chat, "message_id": "IN", "to": peer}
	code, out := e.do(t, "POST", "/api/v1/messages/forward", body)
	if code != 202 || len(e.tr.sent) != 0 {
		t.Fatal(code, out)
	}
	sc, err := e.db.GetScheduled(t.Context(), int64(out["scheduled_id"].(float64)))
	if err != nil {
		t.Fatal(err)
	}
	q.Enabled = false
	req := sender.RequestFromSchedule(sc)
	if _, err = e.db.EditBody(t.Context(), chat, "IN", "changed", time.Now().Unix()); err != nil {
		t.Fatal(err)
	}
	if _, err = e.snd.Send(t.Context(), req); err != sender.ErrSourceChanged || len(e.tr.sent) != 0 {
		t.Fatal(err)
	}
	if _, err = e.db.EditBody(t.Context(), chat, "IN", "agenda", time.Now().Unix()); err != nil {
		t.Fatal(err)
	}
	if res, err := e.snd.Send(t.Context(), req); err != nil || res.ID == "" || !e.tr.sent[0].GetExtendedTextMessage().GetContextInfo().GetIsForwarded() {
		t.Fatal(res, err)
	}
	code, _ = e.do(t, "POST", "/api/v1/send", map[string]string{"to": peer, "text": "agenda"})
	if code != 409 {
		t.Fatal(code)
	}
	if _, err = e.db.EditBody(t.Context(), chat, "IN", "new agenda", time.Now().Unix()); err != nil {
		t.Fatal(err)
	}
	e.snd.Cfg.Limits.PerHour = 2
	code, out = e.do(t, "POST", "/api/v1/messages/forward", body)
	if code != 200 {
		t.Fatal(code, out)
	}
	code, _ = e.do(t, "POST", "/api/v1/send", map[string]string{"to": peer, "text": "over quota"})
	if code != 429 {
		t.Fatal(code)
	}
}
