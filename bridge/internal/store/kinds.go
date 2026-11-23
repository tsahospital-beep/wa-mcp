package store

// Message kinds stored in chat_messages.kind.
const (
	KindText        = "text"
	KindImage       = "image"
	KindVideo       = "video"
	KindAudio       = "audio"
	KindVoice       = "voice"
	KindDocument    = "document"
	KindSticker     = "sticker"
	KindLocation    = "location"
	KindContact     = "contact"
	KindPoll        = "poll"
	KindUnsupported = "unsupported" // a message type the bridge cannot show; body names the type
)

// MessageKinds lists every kind, in the order the API documents them.
var MessageKinds = []string{KindText, KindImage, KindVideo, KindAudio, KindVoice, KindDocument, KindSticker,
	KindLocation, KindContact, KindPoll, KindUnsupported}

// IsAttachment reports whether a kind has a downloadable file.
func IsAttachment(kind string) bool {
	switch kind {
	case KindImage, KindVideo, KindAudio, KindVoice, KindDocument, KindSticker:
		return true
	}
	return false
}

// Placeholder is the short text used for a message in previews and quotes:
// the text itself, or "[document: name]", or "[kind]".
func Placeholder(kind, text, attachmentName string) string {
	if text != "" {
		return text
	}
	switch kind {
	case "", KindText:
		return ""
	case KindDocument:
		if attachmentName != "" {
			return "[document: " + attachmentName + "]"
		}
	}
	return "[" + kind + "]"
}

// Placeholder applies the package rule to a stored message.
func (m *Message) Placeholder() string { return Placeholder(m.Kind, m.Body, m.AttachmentName) }
