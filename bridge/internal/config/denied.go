package config

// denyAllStores adds the store folders of every account to the paths that
// are never sent, so one account cannot send another account's session or
// key files.
func (c *Config) denyAllStores() {
	for _, dir := range c.accountStores {
		if p := c.abs(dir); dir != "" && !contains(c.Sending.DeniedPaths, p) {
			c.Sending.DeniedPaths = append(c.Sending.DeniedPaths, p)
		}
	}
}
