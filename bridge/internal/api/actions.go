package api

import (
	"net/http"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/sender"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

func (s *Server) action(kind string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.Cfg.Sending.Enabled {
			fail(w, http.StatusForbidden, sender.ErrDisabled.Error())
			return
		}
		var body struct {
			Chat         string  `json:"chat"`
			ID           string  `json:"message_id"`
			To           string  `json:"to"`
			Emoji        *string `json:"emoji"`
			Text         *string `json:"text"`
			ExpectedText *string `json:"expected_text"`
		}
		if !decode(w, r, &body) {
			return
		}
		chat, err := wa.ParseTarget(body.Chat)
		if err != nil {
			fail(w, http.StatusBadRequest, err.Error())
			return
		}
		if body.ID == "" {
			fail(w, http.StatusBadRequest, "message_id is required")
			return
		}
		a := wa.MessageAction{Kind: kind, Chat: chat.String(), MessageID: body.ID}
		if kind != "forward" && body.To != "" {
			fail(w, http.StatusBadRequest, "to applies only to forwards")
			return
		}
		to := a.Chat
		switch kind {
		case "react":
			if body.Emoji == nil {
				fail(w, http.StatusBadRequest, "emoji is required")
				return
			}
			a.Value = *body.Emoji
		case "edit":
			if body.Text == nil {
				fail(w, http.StatusBadRequest, "text is required")
				return
			}
			a.Value = *body.Text
		case "forward":
			to = body.To
			if body.ExpectedText != nil {
				a.Value = *body.ExpectedText
			} else {
				m, err := s.DB.GetMessage(r.Context(), a.Chat, a.MessageID)
				if err != nil {
					fail(w, sendStatus(err), err.Error())
					return
				}
				a.Value = m.Body
			}
		}
		s.dispatchSend(w, r, sender.Request{To: to, Action: &a})
	}
}
