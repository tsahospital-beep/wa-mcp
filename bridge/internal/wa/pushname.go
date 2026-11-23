package wa

// PushName is the display name of our own account ("" before login), used to tell
// which sender of an imported chat export is the account owner.
func (c *Client) PushName() string { return c.cur().Store.PushName }
