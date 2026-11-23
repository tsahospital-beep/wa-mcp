// Package common holds small helpers shared by several packages: digit
// handling, masking for logs, CDN paths, loopback checks, timestamps and the
// error type for failures on the WhatsApp side.
package common

import (
	"net"
	"net/url"
	"path"
	"regexp"
	"strings"
	"time"
)

// Digits keeps only the ASCII digits of s.
func Digits(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// MaskNumber keeps the first digit and the last four digits of a phone
// number and replaces the rest with three asterisks in two groups.
// It returns "" for inputs with fewer than 7 digits.
func MaskNumber(s string) string {
	d := Digits(s)
	if len(d) < 7 {
		return ""
	}
	return "+" + d[:1] + " *** *** " + d[len(d)-4:]
}

// MaskJID hides most of the user part of a JID for log lines: the first and
// the last four characters stay, the middle becomes three asterisks;
// the server part is kept.
func MaskJID(jid string) string {
	user, server, found := strings.Cut(jid, "@")
	if i := strings.IndexAny(user, ":."); i >= 0 {
		user = user[:i]
	}
	masked := "***"
	if len(user) >= 7 {
		masked = user[:1] + "***" + user[len(user)-4:]
	}
	if !found {
		return masked
	}
	return masked + "@" + server
}

// CDNPath keeps path and query of a media CDN link; the query carries the signature.
func CDNPath(link string) string {
	u, err := url.Parse(link)
	if err != nil || u.Path == "" || u.Path == "/" {
		return ""
	}
	p := u.EscapedPath()
	if u.RawQuery != "" {
		p += "?" + u.RawQuery
	}
	return p
}

// RedactURL keeps only scheme and host, for log lines.
func RedactURL(link string) string {
	u, err := url.Parse(link)
	if err != nil || u.Host == "" {
		return "(url)"
	}
	return u.Scheme + "://" + u.Host
}

var (
	// quotedPath is an absolute Windows (C:\, \\server) or Unix path in double quotes.
	quotedPath = regexp.MustCompile(`"(?:[A-Za-z]:[\\/]|\\\\|/)[^"]*"`)
	// barePath is an absolute path without spaces after a start, space, quote, bracket or "=".
	barePath = regexp.MustCompile(`(^|[\s'(=])((?:[A-Za-z]:[\\/]|\\\\|/)(?:[^\s"'\\/<>|*?]+[\\/])+[^\s"'\\/<>|*?]*)`)
)

// BaseName strips directory and drive prefixes in either path syntax.
func BaseName(p string) string {
	p = strings.ReplaceAll(p, "\\", "/")
	if len(p) >= 2 && p[1] == ':' && ((p[0] >= 'A' && p[0] <= 'Z') || (p[0] >= 'a' && p[0] <= 'z')) {
		p = p[2:]
	}
	return path.Base(p)
}

// ScrubPaths replaces absolute paths in a message by their base name, for
// logs and stored error texts.
func ScrubPaths(msg string) string {
	msg = quotedPath.ReplaceAllStringFunc(msg, func(q string) string {
		return `"` + BaseName(strings.Trim(q, `"`)) + `"`
	})
	return barePath.ReplaceAllStringFunc(msg, func(m string) string {
		sub := barePath.FindStringSubmatch(m)
		return sub[1] + BaseName(sub[2])
	})
}

// IsLoopbackHost reports whether host (no port) is localhost or a loopback IP.
func IsLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(strings.Trim(host, "[]"))
	return ip != nil && ip.IsLoopback()
}

// IsUnspecifiedHost reports whether host means "all interfaces" (0.0.0.0, ::, or empty).
func IsUnspecifiedHost(host string) bool {
	if host == "" {
		return true
	}
	ip := net.ParseIP(strings.Trim(host, "[]"))
	return ip != nil && ip.IsUnspecified()
}

// MillisThreshold is the shared rule for numeric timestamps (Go and Python):
// an integer greater than 100000000000 is milliseconds, otherwise seconds.
const MillisThreshold = 100_000_000_000

// UnixFromNumber applies MillisThreshold.
func UnixFromNumber(n int64) time.Time {
	if n > MillisThreshold {
		return time.UnixMilli(n)
	}
	return time.Unix(n, 0)
}

// UpstreamError marks a failure reported by WhatsApp or its CDN, as opposed
// to a local problem. The API maps it to 502.
type UpstreamError struct {
	Op  string
	Err error
}

func (e *UpstreamError) Error() string { return "whatsapp " + e.Op + ": " + e.Err.Error() }
func (e *UpstreamError) Unwrap() error { return e.Err }

// Upstream wraps err (nil stays nil).
func Upstream(op string, err error) error {
	if err == nil {
		return nil
	}
	return &UpstreamError{Op: op, Err: err}
}

// WaitTimeout runs wait and returns when it finishes or after d, whichever is first.
func WaitTimeout(wait func(), d time.Duration) bool {
	done := make(chan struct{})
	go func() { wait(); close(done) }()
	select {
	case <-done:
		return true
	case <-time.After(d):
		return false
	}
}
