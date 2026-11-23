package sender

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

// Reply points at the message being answered.
type Reply struct {
	ID      string `json:"id"`
	Author  string `json:"author,omitempty"`
	Preview string `json:"preview,omitempty"`
}

// ParseMentions turns phone numbers or JIDs into JIDs.
func ParseMentions(list []string) ([]types.JID, error) {
	out := make([]types.JID, 0, len(list))
	seen := map[string]bool{}
	for _, s := range list {
		j, err := wa.ParseTarget(s)
		if err != nil {
			return nil, fmt.Errorf("mention %q: %w", s, err)
		}
		if !seen[j.String()] {
			seen[j.String()] = true
			out = append(out, j)
		}
	}
	return out, nil
}

// WithMentionTags makes sure the text contains "@<number>" for every mention,
// which is how WhatsApp shows the highlighted name.
func WithMentionTags(text string, mentions []types.JID) string {
	for _, j := range mentions {
		tag := "@" + j.User
		if strings.Contains(text, tag) {
			continue
		}
		if text == "" || strings.HasSuffix(text, " ") || strings.HasSuffix(text, "\n") {
			text += tag
		} else {
			text += " " + tag
		}
	}
	return text
}

// BuildContext returns the ContextInfo for a reply and mentions (nil when neither is set).
func BuildContext(reply *Reply, mentions []types.JID) *waE2E.ContextInfo {
	if (reply == nil || reply.ID == "") && len(mentions) == 0 {
		return nil
	}
	ci := &waE2E.ContextInfo{}
	if reply != nil && reply.ID != "" {
		ci.StanzaID = proto.String(reply.ID)
		if reply.Author != "" {
			ci.Participant = proto.String(reply.Author)
		}
		ci.QuotedMessage = &waE2E.Message{Conversation: proto.String(reply.Preview)}
	}
	for _, j := range mentions {
		ci.MentionedJID = append(ci.MentionedJID, j.String())
	}
	return ci
}

// TextMessage builds a text message; replies and mentions need the extended form.
func TextMessage(text string, ci *waE2E.ContextInfo) *waE2E.Message {
	if ci == nil {
		return &waE2E.Message{Conversation: proto.String(text)}
	}
	return &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(text), ContextInfo: ci}}
}

// part is one WhatsApp message of a send request.
type part struct {
	kind string   // a store kind: text, image, video, audio, voice, document or sticker
	file *os.File // the checked, open file for media parts
	text string   // text or caption
	ctx  bool     // carries the reply and mentions
}

var extKinds = map[string]string{
	".jpg": store.KindImage, ".jpeg": store.KindImage, ".png": store.KindImage, ".gif": store.KindImage,
	".webp": store.KindImage, ".heic": store.KindImage,
	".mp4": store.KindVideo, ".mov": store.KindVideo, ".3gp": store.KindVideo, ".mkv": store.KindVideo,
	".webm": store.KindVideo, ".avi": store.KindVideo,
	".ogg": store.KindAudio, ".opus": store.KindAudio, ".oga": store.KindAudio, ".mp3": store.KindAudio,
	".m4a": store.KindAudio, ".aac": store.KindAudio, ".wav": store.KindAudio, ".amr": store.KindAudio,
}

var extMimes = map[string]string{
	".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
	".heic": "image/heic", ".mp4": "video/mp4", ".mov": "video/quicktime", ".3gp": "video/3gpp",
	".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
	".ogg": "audio/ogg; codecs=opus", ".opus": "audio/ogg; codecs=opus", ".oga": "audio/ogg",
	".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".amr": "audio/amr",
	".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv", ".zip": "application/zip",
	".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
	".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

func mimeOf(path string) string {
	if m, ok := extMimes[strings.ToLower(filepath.Ext(path))]; ok {
		return m
	}
	return "application/octet-stream"
}

// The rules for which files may be voice notes and stickers, used everywhere.
func isOpusFile(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".ogg", ".opus", ".oga":
		return true
	}
	return false
}

func isStickerFile(path string) bool { return strings.EqualFold(filepath.Ext(path), ".webp") }

// mediaKind decides how a file is sent: as a sticker, a voice note, or by its
// extension as image, video, audio or document.
func mediaKind(path string, asVoice, sticker bool) (string, error) {
	switch {
	case sticker:
		if !isStickerFile(path) {
			return "", ErrStickerFormat
		}
		return store.KindSticker, nil
	case asVoice:
		if !isOpusFile(path) {
			return "", ErrNotOpus
		}
		return store.KindVoice, nil
	}
	if k, ok := extKinds[strings.ToLower(filepath.Ext(path))]; ok {
		return k, nil
	}
	return store.KindDocument, nil
}

// plan splits a request into WhatsApp messages: captioned media carries the
// text; voice notes and stickers cannot, so text goes first as its own message.
func plan(text string, media *os.File, asVoice bool, sticker *os.File) ([]part, error) {
	var parts []part
	hasText := strings.TrimSpace(text) != ""
	if media != nil {
		kind, err := mediaKind(media.Name(), asVoice, false)
		if err != nil {
			return nil, err
		}
		switch kind {
		case store.KindImage, store.KindVideo, store.KindDocument:
			parts = append(parts, part{kind: kind, file: media, text: text, ctx: true})
		default:
			if hasText {
				parts = append(parts, part{kind: store.KindText, text: text, ctx: true})
			}
			parts = append(parts, part{kind: kind, file: media, ctx: !hasText})
		}
	} else if hasText {
		parts = append(parts, part{kind: store.KindText, text: text, ctx: true})
	}
	if sticker != nil {
		if _, err := mediaKind(sticker.Name(), false, true); err != nil {
			return nil, err
		}
		parts = append(parts, part{kind: store.KindSticker, file: sticker, ctx: len(parts) == 0})
	}
	if len(parts) == 0 {
		return nil, ErrEmpty
	}
	return parts, nil
}

// planAs is plan with the AsDocument option: the media file goes as one
// document with the text as its caption (voice notes are never documents).
func planAs(text string, media *os.File, asVoice, asDocument bool, sticker *os.File) ([]part, error) {
	if !asDocument || asVoice || media == nil {
		return plan(text, media, asVoice, sticker)
	}
	parts := []part{{kind: store.KindDocument, file: media, text: text, ctx: true}}
	if sticker != nil {
		if _, err := mediaKind(sticker.Name(), false, true); err != nil {
			return nil, err
		}
		parts = append(parts, part{kind: store.KindSticker, file: sticker})
	}
	return parts, nil
}

func uploadKind(kind string) whatsmeow.MediaType {
	switch kind {
	case store.KindImage, store.KindSticker:
		return whatsmeow.MediaImage
	case store.KindVideo:
		return whatsmeow.MediaVideo
	case store.KindAudio, store.KindVoice:
		return whatsmeow.MediaAudio
	default:
		return whatsmeow.MediaDocument
	}
}

// mediaMessage wraps an uploaded file in the message type for its kind.
func mediaMessage(p part, up whatsmeow.UploadResponse, mime string, voice VoiceInfo, ci *waE2E.ContextInfo) *waE2E.Message {
	switch p.kind {
	case store.KindImage:
		return &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
			Caption: optString(p.text), Mimetype: proto.String(mime),
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: proto.Uint64(up.FileLength),
			ContextInfo: ci,
		}}
	case store.KindVideo:
		return &waE2E.Message{VideoMessage: &waE2E.VideoMessage{
			Caption: optString(p.text), Mimetype: proto.String(mime),
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: proto.Uint64(up.FileLength),
			ContextInfo: ci,
		}}
	case store.KindAudio, store.KindVoice:
		a := &waE2E.AudioMessage{
			Mimetype: proto.String(mime),
			URL:      proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: proto.Uint64(up.FileLength),
			ContextInfo: ci,
		}
		if p.kind == store.KindVoice {
			a.PTT = proto.Bool(true)
			a.Mimetype = proto.String("audio/ogg; codecs=opus")
			a.Seconds = proto.Uint32(voice.Seconds)
			a.Waveform = voice.Waveform
		}
		return &waE2E.Message{AudioMessage: a}
	case store.KindSticker:
		return &waE2E.Message{StickerMessage: &waE2E.StickerMessage{
			Mimetype: proto.String("image/webp"),
			URL:      proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: proto.Uint64(up.FileLength),
			ContextInfo: ci,
		}}
	default:
		name := filepath.Base(p.file.Name())
		return &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
			FileName: proto.String(name), Title: proto.String(name),
			Caption: optString(p.text), Mimetype: proto.String(mime),
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: proto.Uint64(up.FileLength),
			ContextInfo: ci,
		}}
	}
}

func optString(s string) *string {
	if s == "" {
		return nil
	}
	return proto.String(s)
}

// maxVoiceBytes caps voice notes, which are read into memory to find their length.
const maxVoiceBytes = 64 << 20

// build uploads media from the checked file handle and returns the message
// plus the row we store.
func (s *Sender) build(ctx context.Context, p part, ci *waE2E.ContextInfo) (*waE2E.Message, store.Message, VoiceInfo, error) {
	row := store.Message{Kind: p.kind, Body: p.text}
	if p.kind == store.KindText {
		return TextMessage(p.text, ci), row, VoiceInfo{}, nil
	}
	st, err := p.file.Stat()
	if err != nil {
		return nil, row, VoiceInfo{}, err
	}
	if st.Size() > maxFileBytes {
		return nil, row, VoiceInfo{}, ErrTooLarge
	}
	if _, err := p.file.Seek(0, io.SeekStart); err != nil {
		return nil, row, VoiceInfo{}, err
	}
	var src io.Reader = p.file
	var voice VoiceInfo
	if p.kind == store.KindVoice {
		if st.Size() > maxVoiceBytes {
			return nil, row, VoiceInfo{}, ErrTooLarge
		}
		data, err := io.ReadAll(p.file)
		if err != nil {
			return nil, row, VoiceInfo{}, err
		}
		if voice, err = ParseOpus(data); err != nil {
			return nil, row, VoiceInfo{}, err
		}
		src = bytes.NewReader(data)
	}
	up, err := s.Transport.Upload(ctx, src, uploadKind(p.kind))
	if err != nil {
		return nil, row, VoiceInfo{}, fmt.Errorf("upload: %w", err)
	}
	name := p.file.Name()
	mime := mimeOf(name)
	switch p.kind {
	case store.KindDocument:
		row.AttachmentName = filepath.Base(name)
	case store.KindSticker:
		mime = "image/webp"
		row.AttachmentName = wa.StickerName(up.FileSHA256)
	case store.KindVoice:
		mime = "audio/ogg; codecs=opus"
	}
	row.Mime, row.ByteSize = mime, int64(up.FileLength)
	row.DirectPath, row.CDNURL = up.DirectPath, up.URL
	row.MediaKey, row.PlainSHA256, row.EncSHA256 = up.MediaKey, up.FileSHA256, up.FileEncSHA256
	row.LocalPath = name
	return mediaMessage(p, up, mime, voice, ci), row, voice, nil
}
