package common

import (
	"errors"
	"fmt"
	"testing"
)

func TestDigitsAndMasks(t *testing.T) {
	if Digits("+1 (555) 010-0002") != "15550100002" {
		t.Fatal("digits")
	}
	if MaskNumber("15550104567") != "+1 *** *** 4567" || MaskNumber("123") != "" {
		t.Fatal("mask number")
	}
	cases := map[string]string{
		"15550100002@s.whatsapp.net":   "1***0002@s.whatsapp.net",
		"15550100002:7@s.whatsapp.net": "1***0002@s.whatsapp.net",
		"120363000000000001@g.us":      "1***0001@g.us",
		"99990000000001@lid":           "9***0001@lid",
		"15550100002":                  "1***0002",
		"status@broadcast":             "***@broadcast",
	}
	for in, want := range cases {
		if got := MaskJID(in); got != want {
			t.Errorf("MaskJID(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestCDNPath(t *testing.T) {
	if got := CDNPath("https://mmg.whatsapp.net/v/t62/abc.enc?ccb=11-4&oh=sig"); got != "/v/t62/abc.enc?ccb=11-4&oh=sig" {
		t.Fatalf("got %q", got)
	}
	if CDNPath("https://a.whatsapp.net") != "" || CDNPath("::bad") != "" {
		t.Fatal("empty paths must give empty result")
	}
}

func TestHostKinds(t *testing.T) {
	for _, h := range []string{"localhost", "127.0.0.1", "::1", "[::1]", "127.0.0.2"} {
		if !IsLoopbackHost(h) {
			t.Errorf("%s should be loopback", h)
		}
	}
	for _, h := range []string{"0.0.0.0", "192.168.1.5", "example.org"} {
		if IsLoopbackHost(h) {
			t.Errorf("%s is not loopback", h)
		}
	}
	if !IsUnspecifiedHost("0.0.0.0") || !IsUnspecifiedHost("::") || !IsUnspecifiedHost("") || IsUnspecifiedHost("127.0.0.1") {
		t.Fatal("unspecified hosts")
	}
}

func TestUnixFromNumber(t *testing.T) {
	if UnixFromNumber(100_000_000_000).Unix() != 100_000_000_000 {
		t.Fatal("the threshold itself is seconds")
	}
	if got := UnixFromNumber(100_000_000_001); got.UnixMilli() != 100_000_000_001 {
		t.Fatalf("above the threshold is milliseconds: %v", got)
	}
	if UnixFromNumber(1767323045).Unix() != 1767323045 || UnixFromNumber(1767323045123).Unix() != 1767323045 {
		t.Fatal("seconds and milliseconds of the same time")
	}
}

func TestScrubPathsAndRedactURL(t *testing.T) {
	cases := map[string]string{
		`media path "C:\Users\bob\secret\salary review.pdf": file not found`: `media path "salary review.pdf": file not found`,
		`open /home/alice/private/plan.txt: no such file`:                    `open plan.txt: no such file`,
		`stat \\server\share\x\notes.ogg: denied`:                            `stat notes.ogg: denied`,
		`send limit reached: per_hour=60`:                                    `send limit reached: per_hour=60`,
	}
	for in, want := range cases {
		if got := ScrubPaths(in); got != want {
			t.Errorf("ScrubPaths(%q) = %q, want %q", in, got, want)
		}
	}
	if RedactURL("https://user:pw@hooks.example.org:8443/a/b?token=1") != "https://hooks.example.org:8443" {
		t.Fatal("redact")
	}
}

func TestUpstreamError(t *testing.T) {
	base := errors.New("HTTP 500")
	err := fmt.Errorf("send: %w", Upstream("send", base))
	var ue *UpstreamError
	if !errors.As(err, &ue) || !errors.Is(err, base) || ue.Op != "send" {
		t.Fatalf("wrap: %v", err)
	}
	if Upstream("x", nil) != nil {
		t.Fatal("nil must stay nil")
	}
}
