package wa

import (
	"context"
	"time"
)

// PushName is the display name of our own account ("" before login), used to tell
// which sender of an imported chat export is the account owner.
func (c *Client) PushName() string {
	id := c.session().id
	if id.IsEmpty() {
		return ""
	}
	ctx, cancel := context.WithTimeout(c.workCtx, 3*time.Second)
	defer cancel()
	dev, err := c.container.GetDevice(ctx, id)
	if err != nil || dev == nil {
		return ""
	}
	return dev.PushName
}
