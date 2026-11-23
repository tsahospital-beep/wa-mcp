package wa

import (
	"go.mau.fi/whatsmeow/types/events"
)

// whatsmeow does not reconnect by itself after these events (the
// events.PermanentDisconnect class): a remote logout, another client with the
// same keys, a temporary ban, an outdated client, a failed CAT refresh or a
// connect failure. The reason is kept until the next successful connection,
// shown in the status and makes /health report the bridge as unhealthy.

func (c *Client) setDisconnect(reason string) { c.disconnect.Store(reason) }

// DisconnectReason describes the last permanent disconnect ("" while
// connected or when the connection ended in a way whatsmeow recovers from).
func (c *Client) DisconnectReason() string {
	v, _ := c.disconnect.Load().(string)
	return v
}

// permanentDisconnect records the reason. Events without their own
// connection state (client outdated, CAT refresh, connect failure) are
// logged and sent to webhooks as state "permanent_disconnect".
func (c *Client) permanentDisconnect(evt any, reason string) {
	c.setDisconnect(reason)
	switch evt.(type) {
	case *events.StreamReplaced, *events.TemporaryBan, *events.LoggedOut:
		return // these have their own state and log line
	case *events.ClientOutdated:
		c.opts.Log.Printf("WhatsApp refuses this client version; update the bridge")
	default:
		c.opts.Log.Printf("disconnected from WhatsApp for good (%s); restart the bridge after fixing the cause", reason)
	}
	c.emit("connection", map[string]any{"state": "permanent_disconnect", "reason": reason})
}
