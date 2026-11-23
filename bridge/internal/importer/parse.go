// Package importer reads the files WhatsApp's "Export chat" feature writes
// (a .txt file, or a .zip with the .txt and the media) and stores their
// messages in the bridge database, for history older than what the phone syncs.
//
// The parser handles the Android and the iOS layouts in several locales:
// numeric dates in day-month-year, month-day-year or year-month-day order with
// "/", "." or "-" as separators, 24 hour and 12 hour clocks, multi-line
// messages, system lines and media placeholders. Export files carry no time
// zone, so the caller names one (the local zone by default).
package importer

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// Date orders accepted by Options.DateOrder.
const (
	OrderDMY = "dmy"
	OrderMDY = "mdy"
	OrderYMD = "ymd"
)

// Where a date order came from.
const (
	SourceForced      = "forced"
	SourceDetected    = "detected"    // a field above 12 decided
	SourceChronology  = "chronology"  // one order keeps the messages in time order, the other does not
	SourceUnambiguous = "unambiguous" // day and month are equal on every line
	SourceAssumed     = "assumed"     // nothing decided; day-month-year was assumed
)

// ErrDateConflict means the file has a day above 12 in both date positions.
var ErrDateConflict = errors.New("the dates do not fit one order (a value above 12 in both the first and the second field); " +
	"the file may mix formats, set date_order to choose one")

// ErrDateAmbiguous means the order could not be told from the file.
var ErrDateAmbiguous = errors.New("the date order is ambiguous (every date could be day-month or month-day); " +
	"set date_order to dmy, mdy or ymd")

const (
	sp       = `[ \x{00a0}\x{202f}\x{2009}]`
	datePart = `(\d{1,4})[/.\-](\d{1,4})[/.\-](\d{1,4})\.?`
	timePart = `(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?(?:` + sp + `*([AaPp])\.?` + sp + `?[Mm]\.?)?`
)

var (
	androidHeader = regexp.MustCompile(`^` + datePart + `,?` + sp + `*` + timePart + sp + `*[-\x{2013}\x{2014}]` + sp + `+(.*)$`)
	iosHeader     = regexp.MustCompile(`^\[` + datePart + `,?` + sp + `*` + timePart + sp + `*\]` + sp + `*(.*)$`)
)

// header holds the numbers of one message header as written in the file.
type header struct {
	n      [3]int
	w      [3]int // digit counts of the three date fields
	hour   int
	minute int
	second int
	hasSec bool
	marker byte // 'a' or 'p' for a 12 hour clock, 0 for 24 hours
}

// rawMsg is one message before its date order is known.
type rawMsg struct {
	line int // line number of the header
	last int // last line number of the message
	h    header
	rest string   // text of the header line after the header
	cont []string // following lines
}

// isMark reports direction marks and embeddings that exports add around text.
func isMark(r rune) bool {
	return r == 0x200e || r == 0x200f || (r >= 0x202a && r <= 0x202e) || (r >= 0x2066 && r <= 0x2069)
}

func trimMarks(s string) string { return strings.TrimFunc(s, isMark) }

// cleanLine drops byte order marks, direction marks at the start and spaces and marks at the end.
func cleanLine(s string) string {
	if strings.Contains(s, "\uFEFF") {
		s = strings.ReplaceAll(s, "\uFEFF", "")
	}
	s = strings.TrimLeftFunc(s, isMark)
	return strings.TrimRightFunc(s, func(r rune) bool { return isMark(r) || unicode.IsSpace(r) })
}

func atoi(s string) int {
	n, _ := strconv.Atoi(s)
	return n
}

// matchHeader recognises the Android ("d/m/y, h:mm - ") and iOS ("[d/m/y, h:mm:ss] ") header.
func matchHeader(line string) (header, string, bool) {
	var m []string
	if len(line) > 0 && line[0] == '[' {
		m = iosHeader.FindStringSubmatch(line)
	} else {
		m = androidHeader.FindStringSubmatch(line)
	}
	if m == nil {
		return header{}, "", false
	}
	h := header{
		n:      [3]int{atoi(m[1]), atoi(m[2]), atoi(m[3])},
		w:      [3]int{len(m[1]), len(m[2]), len(m[3])},
		hour:   atoi(m[4]),
		minute: atoi(m[5]),
		hasSec: m[6] != "",
		second: atoi(m[6]),
	}
	if m[7] != "" {
		h.marker = byte(strings.ToLower(m[7])[0])
	}
	return h, m[8], true
}

// Sender is one name that wrote messages in the file.
type Sender struct {
	Name     string `json:"name"`
	Messages int    `json:"messages"`
}

// Msg is one message of the file.
type Msg struct {
	Line    int
	At      time.Time
	Sender  string // as written, without marks
	Raw     string // the whole text as written; it is what the stored id is made from
	Text    string // text or caption
	Attach  string // file name of an attachment line
	Omitted string // media placeholder: "media", "image", "video", ... ("" when none)
}

// Skipped counts lines that could not be used and names the first few.
type Skipped struct {
	Count int   `json:"count"`
	First []int `json:"first"`
}

func (s *Skipped) add(n int) {
	s.Count++
	if len(s.First) < 5 {
		s.First = append(s.First, n)
	}
}

// Parsed is the result of reading one export file.
type Parsed struct {
	Messages    []Msg
	Senders     []Sender
	Order       string
	OrderSource string
	SystemLines int
	Skipped     Skipped
}

// parseOptions configure parseText.
type parseOptions struct {
	Order    string // forced order, "" to detect
	Loc      *time.Location
	Omitted  *omittedSet
	HasFile  func(name string) bool // true when the archive holds a file with this name
	Attached map[string]bool        // folded labels of Android "(file attached)" lines
}

// splitLines cuts text into lines without the line ending.
func splitLines(text string) []string {
	lines := strings.Split(text, "\n")
	for i, l := range lines {
		lines[i] = strings.TrimSuffix(l, "\r")
	}
	if n := len(lines); n > 0 && lines[n-1] == "" {
		lines = lines[:n-1]
	}
	return lines
}

// parseText turns an export into messages.
func parseText(text string, o parseOptions) (*Parsed, error) {
	if o.Loc == nil {
		o.Loc = time.Local
	}
	out := &Parsed{Skipped: Skipped{First: []int{}}}
	var msgs []*rawMsg
	var cur *rawMsg
	for i, raw := range splitLines(text) {
		n := i + 1
		line := cleanLine(raw)
		if h, rest, ok := matchHeader(line); ok {
			cur = &rawMsg{line: n, last: n, h: h, rest: rest}
			msgs = append(msgs, cur)
			continue
		}
		if cur == nil {
			if strings.TrimSpace(line) != "" {
				out.Skipped.add(n)
			}
			continue
		}
		cur.cont = append(cur.cont, line)
		cur.last = n
	}
	if len(msgs) == 0 {
		return nil, errors.New("no message headers found: this does not look like a WhatsApp chat export " +
			"(expected lines such as \"1/31/24, 9:05 PM - Name: text\" or \"[31/01/2024, 21:05:00] Name: text\")")
	}

	headers := make([]header, len(msgs))
	for i, m := range msgs {
		headers[i] = m.h
	}
	order, source, err := detectOrder(headers, o.Order, o.Loc)
	if err != nil {
		return nil, err
	}
	out.Order, out.OrderSource = order, source

	counts := map[string]int{}
	var names []string
	for _, m := range msgs {
		at, ok := m.h.time(order, o.Loc)
		if !ok {
			for l := m.line; l <= m.last; l++ {
				out.Skipped.add(l)
			}
			continue
		}
		msg, status := classify(m, o)
		switch status {
		case statusSystem:
			out.SystemLines++
			continue
		case statusEmpty:
			for l := m.line; l <= m.last; l++ {
				out.Skipped.add(l)
			}
			continue
		}
		msg.At = at
		out.Messages = append(out.Messages, msg)
		if _, seen := counts[msg.Sender]; !seen {
			names = append(names, msg.Sender)
		}
		counts[msg.Sender]++
	}
	for _, n := range names {
		out.Senders = append(out.Senders, Sender{Name: n, Messages: counts[n]})
	}
	spreadMinutes(out.Messages, msgs)
	return out, nil
}

// spreadMinutes gives messages that share a minute and carry no seconds one
// second more each (at most 59), so they keep their order in the store, which
// sorts by time.
func spreadMinutes(list []Msg, raws []*rawMsg) {
	bySecond := map[int]bool{}
	for _, r := range raws {
		bySecond[r.line] = r.h.hasSec
	}
	var prev time.Time
	k := 0
	for i := range list {
		if bySecond[list[i].Line] {
			prev, k = time.Time{}, 0
			continue
		}
		if !prev.IsZero() && list[i].At.Equal(prev) {
			k++
		} else {
			prev, k = list[i].At, 0
		}
		if k > 0 {
			list[i].At = prev.Add(time.Duration(min(k, 59)) * time.Second)
		}
	}
}

// time converts the header with the given date order; false when the date does not exist.
func (h header) time(order string, loc *time.Location) (time.Time, bool) {
	var y, mo, d int
	switch order {
	case OrderYMD:
		y, mo, d = h.n[0], h.n[1], h.n[2]
		if h.w[0] <= 2 {
			y = fullYear(y)
		}
	case OrderMDY:
		mo, d, y = h.n[0], h.n[1], h.n[2]
		if h.w[2] <= 2 {
			y = fullYear(y)
		}
	default:
		d, mo, y = h.n[0], h.n[1], h.n[2]
		if h.w[2] <= 2 {
			y = fullYear(y)
		}
	}
	hour := h.hour
	switch h.marker {
	case 'a':
		if hour < 1 || hour > 12 {
			return time.Time{}, false
		}
		hour %= 12
	case 'p':
		if hour < 1 || hour > 12 {
			return time.Time{}, false
		}
		hour = hour%12 + 12
	}
	if hour > 23 || h.minute > 59 || h.second > 59 || y < 1970 || y > 2200 || mo < 1 || mo > 12 || d < 1 {
		return time.Time{}, false
	}
	t := time.Date(y, time.Month(mo), d, hour, h.minute, h.second, 0, loc)
	if t.Day() != d || int(t.Month()) != mo {
		return time.Time{}, false
	}
	return t, true
}

func fullYear(y int) int {
	if y < 70 {
		return 2000 + y
	}
	if y < 100 {
		return 1900 + y
	}
	return y
}

// detectOrder picks the date order: the forced one, year first when a field
// has four digits first, a field above 12 in the first or second place, and
// otherwise the order that keeps the messages in time order.
func detectOrder(hs []header, forced string, loc *time.Location) (order, source string, err error) {
	switch forced {
	case "":
	case OrderDMY, OrderMDY, OrderYMD:
		return forced, SourceForced, nil
	default:
		return "", "", fmt.Errorf("date_order %q: want dmy, mdy or ymd", forced)
	}
	dmy, mdy, ymd := 0, 0, 0
	differs := false
	for _, h := range hs {
		if h.w[0] == 4 || h.n[0] > 31 {
			ymd++
			continue
		}
		if h.n[0] > 12 {
			dmy++
		}
		if h.n[1] > 12 {
			mdy++
		}
		if h.n[0] != h.n[1] {
			differs = true
		}
	}
	switch {
	case ymd > 0 && dmy == 0 && mdy == 0 && ymd == len(hs):
		return OrderYMD, SourceDetected, nil
	case ymd > 0:
		return "", "", ErrDateConflict
	case dmy > 0 && mdy > 0:
		return "", "", ErrDateConflict
	case dmy > 0:
		return OrderDMY, SourceDetected, nil
	case mdy > 0:
		return OrderMDY, SourceDetected, nil
	case !differs:
		return OrderDMY, SourceUnambiguous, nil
	}
	a, b := inversions(hs, OrderDMY, loc), inversions(hs, OrderMDY, loc)
	switch {
	case a < b:
		return OrderDMY, SourceChronology, nil
	case b < a:
		return OrderMDY, SourceChronology, nil
	}
	return OrderDMY, SourceAssumed, nil
}

// inversions counts messages that are earlier than the one before them (or
// have an impossible date) when the file is read with the given order.
func inversions(hs []header, order string, loc *time.Location) int {
	var prev time.Time
	n := 0
	for _, h := range hs {
		t, ok := h.time(order, loc)
		if !ok {
			n++
			continue
		}
		if t.Before(prev) {
			n++
		}
		prev = t
	}
	return n
}

type status int

const (
	statusMessage status = iota
	statusSystem
	statusEmpty
)

// systemVerb matches lines of the form "<name> <did something>" that contain
// a colon only inside the quoted part, so they are not mistaken for "Name: text".
var systemVerb = regexp.MustCompile(`(?i)^[^:]{1,100} (created (group|this group|community)|changed (the|this) (group|subject)|` +
	`changed their phone number|deleted this group's icon|pinned a message|(added|removed|left|joined)( |$))`)

var systemPhrases = []string{
	"messages and calls are end-to-end encrypted",
	"messages to this chat and calls are now secured with end-to-end encryption",
	"messages to this group are now secured with end-to-end encryption",
}

// splitSender cuts "Name: text" at the first colon followed by a space (or
// ending the line). ok is false when the line has no sender.
func splitSender(rest string) (name, text string, ok bool) {
	i := strings.Index(rest, ": ")
	switch {
	case i >= 0:
		name, text = rest[:i], rest[i+2:]
	case strings.HasSuffix(rest, ":"):
		name = strings.TrimSuffix(rest, ":")
	default:
		return "", "", false
	}
	name = cleanName(name)
	if name == "" || utf8.RuneCountInString(name) > 100 {
		return "", "", false
	}
	return name, text, true
}

// cleanName removes marks, a leading "~" (shown before names that are not in the address book) and extra spaces.
func cleanName(s string) string {
	s = strings.ReplaceAll(s, "\uFEFF", "")
	s = trimMarks(s)
	s = strings.TrimLeft(s, "~ \u00A0\u202F")
	return strings.Join(strings.Fields(s), " ")
}

// classify decides what a raw message is: a text or media message, a system line or empty.
func classify(m *rawMsg, o parseOptions) (Msg, status) {
	rest := m.rest
	lower := strings.ToLower(rest)
	for _, p := range systemPhrases {
		if strings.Contains(lower, p) {
			return Msg{}, statusSystem
		}
	}
	if systemVerb.MatchString(trimMarks(rest)) {
		return Msg{}, statusSystem
	}
	name, text, ok := splitSender(rest)
	if !ok {
		return Msg{}, statusSystem
	}
	led := strings.HasPrefix(text, "\u200E")
	text = strings.TrimLeftFunc(text, isMark)

	lines := make([]string, 0, 1+len(m.cont))
	lines = append(lines, text)
	lines = append(lines, m.cont...)
	for len(lines) > 0 && strings.TrimSpace(lines[len(lines)-1]) == "" {
		lines = lines[:len(lines)-1]
	}
	if len(lines) == 0 {
		return Msg{}, statusEmpty
	}
	msg := Msg{Line: m.line, Sender: name, Raw: strings.Join(lines, "\n")}

	media := -1
	for i, l := range lines {
		if file, ok := attachmentLine(l, o); ok {
			msg.Attach, media = file, i
			break
		}
		if typ, ok := o.Omitted.match(l); ok {
			msg.Omitted, media = typ, i
			break
		}
	}
	if media < 0 && led && !strings.HasPrefix(strings.ToLower(text), "location:") {
		// iOS marks its own notes ("You deleted this message", "Missed voice call") with a leading mark.
		return Msg{}, statusSystem
	}
	rest2 := make([]string, 0, len(lines))
	for i, l := range lines {
		if i != media {
			rest2 = append(rest2, l)
		}
	}
	msg.Text = strings.TrimSpace(strings.Join(rest2, "\n"))
	if media < 0 && msg.Text == "" {
		return Msg{}, statusEmpty
	}
	return msg, statusMessage
}
