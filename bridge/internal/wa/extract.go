package wa

import (
	"encoding/hex"
	"fmt"
	"strings"
	"unicode/utf8"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/reflect/protoreflect"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// unwrap strips wrappers that only change delivery (sent from another own
// device, disappearing, view once, document with caption, edit) using the
// same rules whatsmeow applies to incoming events.
func unwrap(m *waE2E.Message) *waE2E.Message {
	if m == nil {
		return nil
	}
	return (&events.Message{RawMessage: m}).UnwrapRaw().Message
}

// Content is what the bridge stores about one message payload.
type Content struct {
	Kind       string // one of store.MessageKinds, "" if nothing to store
	Text       string
	FileName   string
	Mime       string
	Size       uint64
	DirectPath string
	URL        string
	MediaKey   []byte
	PlainHash  []byte
	EncHash    []byte
	Context    *waE2E.ContextInfo
}

// mediaFields is implemented by every downloadable message type.
type mediaFields interface {
	GetMimetype() string
	GetFileLength() uint64
	GetDirectPath() string
	GetURL() string
	GetMediaKey() []byte
	GetFileSHA256() []byte
	GetFileEncSHA256() []byte
	GetContextInfo() *waE2E.ContextInfo
}

func fromMedia(kind string, f mediaFields) Content {
	return Content{
		Kind:       kind,
		Mime:       f.GetMimetype(),
		Size:       f.GetFileLength(),
		DirectPath: f.GetDirectPath(),
		URL:        f.GetURL(),
		MediaKey:   f.GetMediaKey(),
		PlainHash:  f.GetFileSHA256(),
		EncHash:    f.GetFileEncSHA256(),
		Context:    f.GetContextInfo(),
	}
}

// Describe extracts text, caption, attachment and reply context from a message.
func Describe(raw *waE2E.Message) Content {
	m := unwrap(raw)
	if m == nil {
		return Content{}
	}
	if t := m.GetConversation(); t != "" {
		return Content{Kind: store.KindText, Text: t}
	}
	if x := m.GetExtendedTextMessage(); x != nil {
		return Content{Kind: store.KindText, Text: x.GetText(), Context: x.GetContextInfo()}
	}
	if x := m.GetImageMessage(); x != nil {
		c := fromMedia(store.KindImage, x)
		c.Text = x.GetCaption()
		return c
	}
	// A round video note (PTV) is a video message in another field.
	for _, x := range []*waE2E.VideoMessage{m.GetVideoMessage(), m.GetPtvMessage()} {
		if x != nil {
			c := fromMedia(store.KindVideo, x)
			c.Text = x.GetCaption()
			return c
		}
	}
	if x := m.GetAudioMessage(); x != nil {
		kind := store.KindAudio
		if x.GetPTT() {
			kind = store.KindVoice
		}
		return fromMedia(kind, x)
	}
	if x := m.GetDocumentMessage(); x != nil {
		c := fromMedia(store.KindDocument, x)
		c.Text = x.GetCaption()
		c.FileName = x.GetFileName()
		if c.FileName == "" {
			c.FileName = x.GetTitle()
		}
		return c
	}
	if x := m.GetStickerMessage(); x != nil {
		c := fromMedia(store.KindSticker, x)
		if c.Mime == "" {
			c.Mime = "image/webp"
		}
		c.FileName = StickerName(x.GetFileSHA256())
		return c
	}
	if x := m.GetLocationMessage(); x != nil {
		label := strings.TrimSpace(strings.Join(nonEmpty(x.GetName(), x.GetAddress()), ", "))
		return Content{Kind: store.KindLocation, Text: placeText(label, x.GetDegreesLatitude(), x.GetDegreesLongitude()), Context: x.GetContextInfo()}
	}
	if x := m.GetLiveLocationMessage(); x != nil {
		return Content{Kind: store.KindLocation, Text: placeText(x.GetCaption(), x.GetDegreesLatitude(), x.GetDegreesLongitude()), Context: x.GetContextInfo()}
	}
	if x := m.GetContactMessage(); x != nil {
		return Content{Kind: store.KindContact, Text: contactText(x), Context: x.GetContextInfo()}
	}
	if x := m.GetContactsArrayMessage(); x != nil {
		parts := make([]string, 0, len(x.GetContacts()))
		for _, ct := range x.GetContacts() {
			parts = append(parts, contactText(ct))
		}
		return Content{Kind: store.KindContact, Text: strings.Join(parts, "\n"), Context: x.GetContextInfo()}
	}
	for _, p := range []*waE2E.PollCreationMessage{m.GetPollCreationMessage(), m.GetPollCreationMessageV2(),
		m.GetPollCreationMessageV3(), m.GetPollCreationMessageV5()} {
		if p != nil {
			lines := []string{p.GetName()}
			for _, o := range p.GetOptions() {
				lines = append(lines, "- "+o.GetOptionName())
			}
			return Content{Kind: store.KindPoll, Text: strings.Join(lines, "\n"), Context: p.GetContextInfo()}
		}
	}
	if name := unsupportedType(m); name != "" {
		return Content{Kind: store.KindUnsupported, Text: "[unsupported message: " + name + "]"}
	}
	return Content{}
}

// notContent are fields that carry no message of their own (keys, metadata,
// empty text) or actions handled elsewhere or deliberately not stored (votes, pins).
var notContent = map[protoreflect.Name]bool{
	"conversation": true, "extendedTextMessage": true,
	"messageContextInfo": true, "senderKeyDistributionMessage": true, "fastRatchetKeySenderKeyDistributionMessage": true,
	"protocolMessage": true, "reactionMessage": true, "encReactionMessage": true, "pollUpdateMessage": true,
	"keepInChatMessage": true, "pinInChatMessage": true,
}

// unsupportedType names the first content field of a message the bridge
// does not understand, or "" when there is none.
func unsupportedType(m *waE2E.Message) string {
	name := ""
	m.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		if notContent[fd.Name()] {
			return true
		}
		name = string(fd.Name())
		return false
	})
	return name
}

func nonEmpty(values ...string) []string {
	out := values[:0:0]
	for _, v := range values {
		if v = strings.TrimSpace(v); v != "" {
			out = append(out, v)
		}
	}
	return out
}

func placeText(label string, lat, lon float64) string {
	coords := fmt.Sprintf("%.5f, %.5f", lat, lon)
	if label == "" {
		return coords
	}
	return label + " (" + coords + ")"
}

// contactText is the display name plus the phone numbers of a shared contact card.
func contactText(c *waE2E.ContactMessage) string {
	var phones []string
	for _, line := range strings.Split(strings.ReplaceAll(c.GetVcard(), "\r\n", "\n"), "\n") {
		if !strings.HasPrefix(strings.ToUpper(line), "TEL") {
			continue
		}
		if i := strings.LastIndex(line, ":"); i >= 0 {
			if v := strings.TrimSpace(line[i+1:]); v != "" {
				phones = append(phones, v)
			}
		}
	}
	name := strings.TrimSpace(c.GetDisplayName())
	if len(phones) == 0 {
		return name
	}
	if name == "" {
		return strings.Join(phones, ", ")
	}
	return name + ": " + strings.Join(phones, ", ")
}

// StickerName is a stable file name for a sticker: the same sticker always
// gets the same name, wherever and whenever it was sent.
func StickerName(plainHash []byte) string {
	n := len(plainHash)
	if n > 8 {
		n = 8
	}
	if n == 0 {
		return "sticker.webp"
	}
	return "sticker_" + hex.EncodeToString(plainHash[:n]) + ".webp"
}

// Placeholder is the short text used for a message in previews.
func (c Content) Placeholder() string { return store.Placeholder(c.Kind, c.Text, c.FileName) }

// Reply is the context of a quoted message.
type Reply struct {
	ID      string
	Author  string
	Preview string
}

const previewRunes = 200

// ReplyOf reads the quoted message id, its author and a short preview.
func ReplyOf(ci *waE2E.ContextInfo) Reply {
	if ci.GetStanzaID() == "" {
		return Reply{}
	}
	r := Reply{ID: ci.GetStanzaID(), Author: ci.GetParticipant()}
	if q := ci.GetQuotedMessage(); q != nil {
		r.Preview = Shorten(Describe(q).Placeholder(), previewRunes)
	}
	return r
}

// Shorten cuts s to at most n runes, marking the cut with "...".
func Shorten(s string, n int) string {
	s = strings.TrimSpace(s)
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return strings.TrimSpace(string(r[:n])) + "..."
}
