package config

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
)

// LoginPublic serves the login window behind a TLS reverse proxy, for a bridge
// on a server. With URL empty the window stays on 127.0.0.1 (the default).
type LoginPublic struct {
	// URL is the https address of the login page as the proxy publishes it,
	// for example https://wa.example.com/login/. Its path is the page prefix.
	URL string `json:"url"`
	// Listen is the host:port the proxy forwards to.
	Listen string `json:"listen"`
}

// Enabled reports whether the login window is served for a proxy.
func (p LoginPublic) Enabled() bool { return p.URL != "" }

// validate normalizes URL (trailing slash on the path) and checks both values.
func (p *LoginPublic) validate() error {
	p.URL = strings.TrimSpace(p.URL)
	if p.URL == "" {
		return nil
	}
	u, err := url.Parse(p.URL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("login.public.url: need an absolute URL without user info, query or fragment")
	}
	if u.Scheme != "https" && !(u.Scheme == "http" && isLoopback(u.Hostname())) {
		return errors.New("login.public.url: must be https (http only for a loopback address)")
	}
	if !strings.HasSuffix(u.Path, "/") {
		u.Path += "/"
	}
	p.URL = u.String()
	if _, _, err := net.SplitHostPort(p.Listen); err != nil {
		return fmt.Errorf("login.public.listen: %v", err)
	}
	return nil
}

func isLoopback(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
