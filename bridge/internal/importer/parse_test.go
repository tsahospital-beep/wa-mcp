package importer

import (
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
)

func testOptions(order string) parseOptions {
	return parseOptions{Order: order, Loc: time.UTC, Omitted: newOmittedSet(nil), Attached: attachedSet()}
}

func mustParse(t *testing.T, text, order string) *Parsed {
	t.Helper()
	p, err := parseText(text, testOptions(order))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return p
}

func TestHeaderFormats(t *testing.T) {
	cases := []struct {
		name string
		line string
		want string
	}{
		{"android us 12h", "1/31/24, 9:05 PM - Alice: x", "2024-01-31T21:05:00Z"},
		{"narrow no-break space before PM", "1/31/24, 9:05\u202fPM - Alice: x", "2024-01-31T21:05:00Z"},
		{"no-break space before PM", "1/31/24, 9:05\u00a0PM - Alice: x", "2024-01-31T21:05:00Z"},
		{"lowercase pm", "1/31/24, 9:05 pm - Alice: x", "2024-01-31T21:05:00Z"},
		{"dotted p.m.", "1/31/24, 9:05 p.m. - Alice: x", "2024-01-31T21:05:00Z"},
		{"spaced p. m.", "1/31/24, 9:05 p. m. - Alice: x", "2024-01-31T21:05:00Z"},
		{"narrow space a. m.", "1/31/24, 9:05\u202fa. m. - Alice: x", "2024-01-31T09:05:00Z"},
		{"midnight", "1/31/24, 12:05 AM - Alice: x", "2024-01-31T00:05:00Z"},
		{"noon", "1/31/24, 12:05 PM - Alice: x", "2024-01-31T12:05:00Z"},
		{"android dmy dots", "31.01.2024, 21:05 - Alice: x", "2024-01-31T21:05:00Z"},
		{"android dmy dashes without comma", "31-01-2024 21:05 - Alice: x", "2024-01-31T21:05:00Z"},
		{"trailing dot after the year", "31.1.24. 21:05 - Alice: x", "2024-01-31T21:05:00Z"},
		{"android ymd dashes", "2024-01-31, 21:05 - Alice: x", "2024-01-31T21:05:00Z"},
		{"android ymd slashes with seconds", "2024/01/31, 21:05:09 - Alice: x", "2024-01-31T21:05:09Z"},
		{"dot as time separator", "31/01/24 21.05 - Alice: x", "2024-01-31T21:05:00Z"},
		{"space before the colon", "31/01/2024, 21:05 - Alice : x", "2024-01-31T21:05:00Z"},
		{"en dash separator", "31/1/24, 21:05 \u2013 Alice: x", "2024-01-31T21:05:00Z"},
		{"ios dmy", "[31/01/2024, 21:05:09] Alice: x", "2024-01-31T21:05:09Z"},
		{"ios dots 12h", "[31.01.24, 9:05:09 PM] Alice: x", "2024-01-31T21:05:09Z"},
		{"ios ymd", "[2024-01-31, 21:05:09] Alice: x", "2024-01-31T21:05:09Z"},
		{"ios mdy narrow space", "[1/31/24, 9:05:09\u202fAM] Alice: x", "2024-01-31T09:05:09Z"},
		{"bom and left-to-right mark", "\uFEFF\u200e[31/01/2024, 21:05:09] Alice: x", "2024-01-31T21:05:09Z"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := mustParse(t, c.line+"\n", "")
			if len(p.Messages) != 1 {
				t.Fatalf("got %d messages, system %d, skipped %v", len(p.Messages), p.SystemLines, p.Skipped)
			}
			got := p.Messages[0]
			if got.At.UTC().Format(time.RFC3339) != c.want {
				t.Errorf("time %s, want %s", got.At.UTC().Format(time.RFC3339), c.want)
			}
			if got.Sender != "Alice" || got.Text != "x" {
				t.Errorf("sender %q text %q", got.Sender, got.Text)
			}
		})
	}
}

func TestTimeZoneApplied(t *testing.T) {
	opts := testOptions("")
	opts.Loc = time.FixedZone("plus3", 3*3600)
	p, err := parseText("31/01/2024, 21:05 - Alice: x\n", opts)
	if err != nil {
		t.Fatal(err)
	}
	if got := p.Messages[0].At.UTC().Format(time.RFC3339); got != "2024-01-31T18:05:00Z" {
		t.Fatalf("got %s", got)
	}
}

const androidChat = `31/01/2024, 21:00 - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them. Learn more.
31/01/2024, 21:05 - Alice: Hello Bob
31/01/2024, 21:06 - Bob: Hi Alice
second line of the message

fourth line after a blank line
31/01/2024, 21:07 - Alice created group "Book club: 2024"
31/01/2024, 21:08 - Alice: <Media omitted>
31/01/2024, 21:09 - Bob: IMG-20240131-WA0001.jpg (file attached)
Look at this
31/01/2024, 21:10 - Alice added Carol
31/01/2024, 21:11 - Missed voice call
31/01/2024, 21:12 - Bob: Meet at 5: bring the notes
`

func TestAndroidChat(t *testing.T) {
	p := mustParse(t, androidChat, "")
	if p.Order != OrderDMY || p.OrderSource != SourceDetected {
		t.Fatalf("order %s from %s", p.Order, p.OrderSource)
	}
	if p.SystemLines != 4 {
		t.Errorf("system lines %d, want 4", p.SystemLines)
	}
	if len(p.Messages) != 5 {
		t.Fatalf("messages %d: %+v", len(p.Messages), p.Messages)
	}
	m := p.Messages
	if m[1].Text != "Hi Alice\nsecond line of the message\n\nfourth line after a blank line" {
		t.Errorf("multi-line text %q", m[1].Text)
	}
	if m[2].Omitted != "media" || m[2].Text != "" {
		t.Errorf("omitted line: %+v", m[2])
	}
	if m[3].Attach != "IMG-20240131-WA0001.jpg" || m[3].Text != "Look at this" {
		t.Errorf("attachment with a caption: %+v", m[3])
	}
	if m[4].Text != "Meet at 5: bring the notes" || m[4].Sender != "Bob" {
		t.Errorf("colon in text: %+v", m[4])
	}
	want := []Sender{{"Alice", 2}, {"Bob", 3}}
	if !reflect.DeepEqual(p.Senders, want) {
		t.Errorf("senders %v", p.Senders)
	}
}

func TestAttachmentNeedsLabelOrFile(t *testing.T) {
	text := "31/01/2024, 21:09 - Bob: IMG-1.jpg (file attached)\nCaption\n31/01/2024, 21:10 - Bob: DOC-2.pdf (Datei angeh\u00e4ngt)\n" +
		"31/01/2024, 21:11 - Bob: VID-3.mp4 (zalacznik)\n31/01/2024, 21:12 - Bob: notes.txt (draft)\n"
	opts := testOptions("")
	opts.HasFile = func(name string) bool { return name == "VID-3.mp4" }
	p, err := parseText(text, opts)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, m := range p.Messages {
		got = append(got, m.Attach+"|"+m.Text)
	}
	want := []string{"IMG-1.jpg|Caption", "DOC-2.pdf|", "VID-3.mp4|", "|notes.txt (draft)"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestIOSChat(t *testing.T) {
	text := "\uFEFF[31/01/2024, 21:00:00] Book club: \u200eMessages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them.\r\n" +
		"[31/01/2024, 21:06:10] Alice: Hello\r\n" +
		"[31/01/2024, 21:07:11] Bob: \u200e<attached: 00000012-PHOTO-2024-01-31-21-07-11.jpg>\r\n" +
		"[31/01/2024, 21:08:12] Alice: \u200eimage omitted\r\n" +
		"[31/01/2024, 21:09:00] Alice: \u200eThis message was deleted.\r\n" +
		"[31/01/2024, 21:10:00] \u200eAlice added Carol\r\n" +
		"[31/01/2024, 21:11:00] Bob: \u200elocation: https://maps.example/?q=1,2\r\n" +
		"[31/01/2024, 21:12:00] Bob: Caption first\r\n\u200e<attached: 00000013-PHOTO-2024-01-31-21-12-00.jpg>\r\n"
	p := mustParse(t, text, "")
	if p.SystemLines != 3 {
		t.Errorf("system lines %d, want 3", p.SystemLines)
	}
	if len(p.Messages) != 5 {
		t.Fatalf("messages %d: %+v", len(p.Messages), p.Messages)
	}
	if p.Messages[1].Attach != "00000012-PHOTO-2024-01-31-21-07-11.jpg" || p.Messages[1].Text != "" {
		t.Errorf("attached: %+v", p.Messages[1])
	}
	if p.Messages[2].Omitted != "image" {
		t.Errorf("omitted: %+v", p.Messages[2])
	}
	if !strings.HasPrefix(p.Messages[3].Text, "location:") {
		t.Errorf("location kept as text: %+v", p.Messages[3])
	}
	if p.Messages[4].Attach == "" || p.Messages[4].Text != "Caption first" {
		t.Errorf("caption and attachment: %+v", p.Messages[4])
	}
	if got := p.Messages[0].At.Format("15:04:05"); got != "21:06:10" {
		t.Errorf("seconds kept: %s", got)
	}
}

func TestOmittedPhrasesInOtherLanguages(t *testing.T) {
	set := newOmittedSet([]string{"<Custom placeholder>"})
	for _, line := range []string{
		"<Media omitted>", "<media omitted>", "\u200eimage omitted", "<Multimedia omitido>", "<M\u00eddia oculta>",
		"<Medien ausgeschlossen>", "<M\u00e9dias omis>", "<Media omessi>", "Bild weggelassen", "<Custom placeholder>",
		"<\u0411\u0435\u0437 \u043c\u0435\u0434\u0438\u0430\u0444\u0430\u0439\u043b\u043e\u0432>",
	} {
		if _, ok := set.match(line); !ok {
			t.Errorf("%q is not recognised", line)
		}
	}
	if _, ok := set.match("omitted images are fine"); ok {
		t.Error("a sentence that contains the words must not match")
	}
}

func TestDateOrderDetection(t *testing.T) {
	cases := []struct {
		name   string
		lines  string
		forced string
		order  string
		source string
		err    error
	}{
		{"day above 12 first", "13/01/24, 10:00 - A: x\n02/01/24, 10:00 - A: y\n", "", OrderDMY, SourceDetected, nil},
		{"day above 12 second", "01/13/24, 10:00 - A: x\n01/02/24, 10:00 - A: y\n", "", OrderMDY, SourceDetected, nil},
		{"year first", "2024-01-02, 10:00 - A: x\n", "", OrderYMD, SourceDetected, nil},
		{"same day and month", "03/03/24, 10:00 - A: x\n", "", OrderDMY, SourceUnambiguous, nil},
		{"chronology picks dmy", "01/03/24, 10:00 - A: x\n02/03/24, 10:00 - A: x\n03/03/24, 10:00 - A: x\n01/04/24, 10:00 - A: x\n", "", OrderDMY, SourceChronology, nil},
		{"chronology picks mdy", "03/01/24, 10:00 - A: x\n03/02/24, 10:00 - A: x\n03/03/24, 10:00 - A: x\n04/01/24, 10:00 - A: x\n", "", OrderMDY, SourceChronology, nil},
		{"assumed", "03/04/24, 10:00 - A: x\n04/04/24, 10:00 - A: x\n", "", OrderDMY, SourceAssumed, nil},
		{"forced mdy", "03/04/24, 10:00 - A: x\n", OrderMDY, OrderMDY, SourceForced, nil},
		{"mixed", "13/01/24, 10:00 - A: x\n01/13/24, 10:00 - A: y\n", "", "", "", ErrDateConflict},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p, err := parseText(c.lines, testOptions(c.forced))
			if !errors.Is(err, c.err) {
				t.Fatalf("error %v, want %v", err, c.err)
			}
			if err != nil {
				return
			}
			if p.Order != c.order || p.OrderSource != c.source {
				t.Errorf("order %s from %s, want %s from %s", p.Order, p.OrderSource, c.order, c.source)
			}
		})
	}
	p := mustParse(t, "03/04/24, 10:00 - A: x\n", OrderMDY)
	if got := p.Messages[0].At.Format("2006-01-02"); got != "2024-03-04" {
		t.Errorf("forced mdy gave %s", got)
	}
	if _, err := parseText("03/04/24, 10:00 - A: x\n", testOptions("dmx")); err == nil {
		t.Error("an unknown date order must be refused")
	}
}

func TestSkippedLinesAndImpossibleDates(t *testing.T) {
	text := "stray line before any header\n\n" +
		"31/01/2024, 21:05 - Alice: fine\n" +
		"31/02/2024, 21:05 - Alice: no such day\ncontinuation of the bad one\n" +
		"31/01/2024, 25:05 - Alice: no such hour\n" +
		"31/01/2024, 21:06 - Alice:\n" +
		"31/01/2024, 21:07 - Bob: last\n"
	p := mustParse(t, text, OrderDMY)
	if len(p.Messages) != 2 {
		t.Fatalf("messages %d", len(p.Messages))
	}
	if p.Skipped.Count != 5 || !reflect.DeepEqual(p.Skipped.First, []int{1, 4, 5, 6, 7}) {
		t.Errorf("skipped %+v", p.Skipped)
	}
}

func TestSkippedKeepsOnlyFirstFive(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 8; i++ {
		b.WriteString("stray\n")
	}
	b.WriteString("31/01/2024, 21:05 - Alice: ok\n")
	p := mustParse(t, b.String(), "")
	if p.Skipped.Count != 8 || len(p.Skipped.First) != 5 {
		t.Errorf("skipped %+v", p.Skipped)
	}
}

func TestNoHeaders(t *testing.T) {
	if _, err := parseText("hello\nworld\n", testOptions("")); err == nil {
		t.Fatal("a file without headers must be refused")
	}
}

func TestMinuteOrderIsKept(t *testing.T) {
	text := "31/01/2024, 21:05 - A: one\n31/01/2024, 21:05 - B: two\n31/01/2024, 21:05 - A: three\n" +
		"[31/01/2024, 21:06:00] A: four\n[31/01/2024, 21:06:00] B: five\n"
	p := mustParse(t, text, OrderDMY)
	var secs []int
	for _, m := range p.Messages {
		secs = append(secs, m.At.Second())
	}
	if !reflect.DeepEqual(secs, []int{0, 1, 2, 0, 0}) {
		t.Fatalf("seconds %v: messages without seconds keep their order, messages with seconds are untouched", secs)
	}
}

func TestSenderNames(t *testing.T) {
	text := "31/01/2024, 21:05 - ~ Alice Example: hi\n31/01/2024, 21:06 - \u200eAlice Example: again\n" +
		"31/01/2024, 21:07 - +1 555 010 2222: from a number\n"
	p := mustParse(t, text, "")
	if len(p.Senders) != 2 || p.Senders[0].Name != "Alice Example" || p.Senders[0].Messages != 2 {
		t.Fatalf("senders %+v", p.Senders)
	}
}

func TestDecodeText(t *testing.T) {
	if got := decodeText([]byte{0xef, 0xbb, 0xbf, 'a'}); got != "a" {
		t.Errorf("utf-8 bom: %q", got)
	}
	if got := decodeText([]byte{0xff, 0xfe, 'a', 0, 'b', 0}); got != "ab" {
		t.Errorf("utf-16 le: %q", got)
	}
	if got := decodeText([]byte{0xfe, 0xff, 0, 'a', 0, 'b'}); got != "ab" {
		t.Errorf("utf-16 be: %q", got)
	}
	if got := decodeText([]byte{'a', 0xff, 'b'}); !strings.HasPrefix(got, "a") || !strings.HasSuffix(got, "b") {
		t.Errorf("invalid utf-8: %q", got)
	}
}

func TestFileKind(t *testing.T) {
	cases := map[string]string{
		"IMG-20240101-WA0001.jpg": "image", "x.PNG": "image", "STK-20240101-WA0002.webp": "sticker", "photo.webp": "image",
		"VID-1.mp4": "video", "PTT-20240101-WA0003.opus": "voice", "00000013-AUDIO-2024.opus": "voice", "AUD-1.mp3": "audio",
		"doc.pdf": "document", "archive.zip": "document", "noext": "document",
	}
	for name, want := range cases {
		if got, _ := fileKind(name); got != want {
			t.Errorf("%s: %s, want %s", name, got, want)
		}
	}
}

func TestTitleHint(t *testing.T) {
	cases := map[string]string{
		"WhatsApp Chat with Alice Example.zip": "Alice Example",
		"WhatsApp Chat - Book club.zip":        "Book club",
		"WhatsApp Chat with Bob (1).txt":       "Bob",
		"WhatsApp_Chat_with_Carol.zip":         "Carol",
		"_chat.txt":                            "",
		"notes.txt":                            "",
	}
	for in, want := range cases {
		if got := titleHint(in); got != want {
			t.Errorf("%s: %q, want %q", in, got, want)
		}
	}
}

func FuzzParseText(f *testing.F) {
	f.Add(androidChat)
	f.Add("[31/01/2024, 21:05:09] Alice: x\r\n\u200e<attached: a.jpg>\r\n")
	f.Add("1/31/24, 9:05\u202fPM - Alice: ")
	f.Add("99/99/99, 99:99 - : \n\n\n:")
	f.Fuzz(func(t *testing.T, text string) {
		p, err := parseText(text, testOptions(""))
		if err != nil {
			return
		}
		for _, m := range p.Messages {
			if m.Sender == "" || m.At.IsZero() {
				t.Fatalf("incomplete message %+v", m)
			}
		}
	})
}

func TestLargeFileIsFast(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 200000; i++ {
		d := 1 + i/20000
		b.WriteString("1" + string(rune('0'+d%9)) + "/01/2024, 10:00 - Alice: message number " + strings.Repeat("x", i%40) + "\n")
		if i%7 == 0 {
			b.WriteString("a continuation line\n")
		}
	}
	start := time.Now()
	p := mustParse(t, b.String(), OrderDMY)
	if len(p.Messages) < 100000 {
		t.Fatalf("only %d messages parsed", len(p.Messages))
	}
	if d := time.Since(start); d > 10*time.Second {
		t.Fatalf("parsing took %s", d)
	}
}
