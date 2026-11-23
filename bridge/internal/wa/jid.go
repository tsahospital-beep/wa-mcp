package wa

import (
	"errors"
	"strings"

	"go.mau.fi/whatsmeow/types"
)

var ErrBadTarget = errors.New("expected a JID (like 15550100001@s.whatsapp.net, ...@lid or ...@g.us) or a phone number with country code")

// validPhone is 7 to 15 digits without a leading zero.
func validPhone(s string) bool {
	if len(s) < 7 || len(s) > 15 || s[0] == '0' {
		return false
	}
	return allDigits(s)
}

func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// validGroup accepts current group ids (digits) and old ones (digits-digits).
func validGroup(s string) bool {
	if len(s) > 40 {
		return false
	}
	head, tail, found := strings.Cut(s, "-")
	if !found {
		return allDigits(s)
	}
	return allDigits(head) && allDigits(tail)
}

// ParseTarget is for sending: a phone number (digits, optional +, spaces,
// dashes, brackets, dots) or a JID on s.whatsapp.net, lid or g.us with a sane
// user part.
func ParseTarget(s string) (types.JID, error) { return parseJID(s, false) }

// ParseChat is for reading stored data: like ParseTarget, and also accepts
// addresses on the hosted and hosted.lid servers (business accounts hosted
// by Meta), which the bridge stores but never sends to.
func ParseChat(s string) (types.JID, error) { return parseJID(s, true) }

func parseJID(s string, hosted bool) (types.JID, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return types.JID{}, ErrBadTarget
	}
	if strings.Contains(s, "@") {
		j, err := types.ParseJID(s)
		if err != nil {
			return types.JID{}, ErrBadTarget
		}
		j = j.ToNonAD()
		ok := false
		switch j.Server {
		case types.DefaultUserServer:
			ok = validPhone(j.User)
		case types.HiddenUserServer:
			ok = allDigits(j.User) && len(j.User) <= 20
		case types.GroupServer:
			ok = validGroup(j.User)
		case types.HostedServer, types.HostedLIDServer:
			ok = hosted && allDigits(j.User) && len(j.User) <= 20
		}
		if !ok {
			return types.JID{}, ErrBadTarget
		}
		return j, nil
	}
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '+' || r == ' ' || r == '-' || r == '(' || r == ')' || r == '.':
		default:
			return types.JID{}, ErrBadTarget
		}
	}
	if !validPhone(b.String()) {
		return types.JID{}, ErrBadTarget
	}
	return types.NewJID(b.String(), types.DefaultUserServer), nil
}
