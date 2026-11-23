package wa

import (
	"errors"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

// MessageAction identifies a stored source. For forwards, Value is the approved copy of its text.
type MessageAction struct {
	Kind      string
	Chat      string
	MessageID string
	Value     string
}

func (c *Client) BuildAction(a MessageAction, chat, author types.JID) (*waE2E.Message, error) {
	wm, err := c.ready()
	if err != nil {
		return nil, err
	}
	return a.Build(wm, chat, author)
}

func (a MessageAction) Build(wm *whatsmeow.Client, chat, author types.JID) (*waE2E.Message, error) {
	switch a.Kind {
	case "react":
		return wm.BuildReaction(chat, author, a.MessageID, a.Value), nil
	case "edit":
		return wm.BuildEdit(chat, a.MessageID, &waE2E.Message{Conversation: new(a.Value)}), nil
	case "delete":
		return wm.BuildRevoke(chat, types.EmptyJID, a.MessageID), nil
	case "forward":
		return &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: new(a.Value), ContextInfo: &waE2E.ContextInfo{IsForwarded: new(true), ForwardingScore: new(uint32(1))},
		}}, nil
	}
	return nil, errors.New("unknown message action")
}
