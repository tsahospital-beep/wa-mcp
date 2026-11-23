// Package media saves attachments of stored messages to disk. When the CDN
// copy has expired it asks the phone to upload the file again and retries.
package media

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

var (
	ErrNotMedia   = errors.New("message has no attachment")
	ErrIncomplete = errors.New("attachment details are incomplete; ask for history again to refresh them")
	ErrNoAvatar   = errors.New("no profile picture")
	ErrTooLarge   = errors.New("attachment is larger than media.max_download_mb")
)

// DefaultMaxBytes is used when Fetcher.MaxBytes is zero.
const DefaultMaxBytes = 512 << 20

// Source is the network side (implemented by wa.Client).
type Source interface {
	DownloadMediaToFile(ctx context.Context, directPath string, encHash, plainHash, key []byte,
		kind whatsmeow.MediaType, file whatsmeow.File) error
	RequestReupload(ctx context.Context, m *store.Message) (string, error)
}

// Fetcher downloads attachments into Dir/<chat>/.
type Fetcher struct {
	DB       *store.DB
	Src      Source
	Dir      string
	MaxBytes int64
	// Private names documents by message id only (set when the store is encrypted).
	Private bool
}

// Result describes a saved attachment.
type Result struct {
	Path           string `json:"path"`
	Kind           string `json:"kind"`
	AttachmentName string `json:"attachment_name,omitempty"`
}

// Expired reports whether a download error means the CDN copy is gone.
func Expired(err error) bool {
	return errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410)
}

func (f *Fetcher) limit() int64 {
	if f.MaxBytes > 0 {
		return f.MaxBytes
	}
	return DefaultMaxBytes
}

// Fetch saves the attachment of message id in chat conv and returns its path.
func (f *Fetcher) Fetch(ctx context.Context, conv, id string) (*Result, error) {
	return f.fetch(ctx, conv, id, "")
}

// FetchTo saves the attachment to target, a file path chosen by the caller,
// unless a file of the expected size is already there. The stored local path
// of the message is not changed.
func (f *Fetcher) FetchTo(ctx context.Context, conv, id, target string) (*Result, error) {
	if target == "" {
		return nil, errors.New("FetchTo needs a target path")
	}
	return f.fetch(ctx, conv, id, target)
}

func (f *Fetcher) fetch(ctx context.Context, conv, id, target string) (*Result, error) {
	m, err := f.DB.GetMessage(ctx, conv, id)
	if err != nil {
		return nil, err
	}
	if !m.IsMedia() {
		return nil, ErrNotMedia
	}
	res := &Result{Kind: m.Kind, AttachmentName: m.AttachmentName}
	own := target == ""
	if m.ByteSize > f.limit() {
		return nil, fmt.Errorf("%w (%d MB)", ErrTooLarge, m.ByteSize>>20)
	}
	dir := filepath.Dir(target)
	if own {
		dir = f.Dir
		if m.LocalPath != "" && sameFile(m.LocalPath, m.ByteSize, m.PlainSHA256, dir) {
			res.Path = m.LocalPath
			return res, nil
		}
		target = filepath.Join(dir, SafeName(m.Conv), FileName(m, f.Private))
	}
	if sameFile(target, m.ByteSize, m.PlainSHA256, dir) {
		if own {
			if err := f.DB.SetLocalPath(ctx, m.Conv, m.ID, target); err != nil {
				return nil, err
			}
		}
		res.Path = target
		return res, nil
	}

	path := m.DirectPath
	if path == "" {
		path = common.CDNPath(m.CDNURL)
	}
	if path == "" || len(m.MediaKey) == 0 {
		return nil, ErrIncomplete
	}
	tmp, err := fsutil.CreateTemp(dir, ".part-*")
	if err != nil {
		return nil, err
	}
	keep := false
	defer func() {
		tmp.Close()
		if !keep {
			fsutil.Wipe(tmp.Name())
		}
	}()
	kind := MediaType(m.Kind)
	capped := &limitedFile{File: tmp, max: f.limit() + downloadSlack}
	err = f.Src.DownloadMediaToFile(ctx, path, m.EncSHA256, m.PlainSHA256, m.MediaKey, kind, capped)
	if err != nil && Expired(err) {
		fresh, rerr := f.Src.RequestReupload(ctx, m)
		if rerr != nil {
			return nil, fmt.Errorf("%w; re-upload from the phone failed: %w", err, rerr)
		}
		_ = f.DB.SetDirectPath(ctx, m.Conv, m.ID, fresh)
		if err := rewind(tmp); err != nil {
			return nil, err
		}
		err = f.Src.DownloadMediaToFile(ctx, fresh, m.EncSHA256, m.PlainSHA256, m.MediaKey, kind, capped)
	}
	if err != nil {
		if errors.Is(err, ErrTooLarge) {
			return nil, ErrTooLarge
		}
		return nil, fmt.Errorf("download: %w", err)
	}
	st, err := tmp.Stat()
	if err != nil {
		return nil, err
	}
	if st.Size() > f.limit() {
		return nil, fmt.Errorf("%w (%d MB)", ErrTooLarge, st.Size()>>20)
	}
	if err := tmp.Close(); err != nil {
		return nil, err
	}
	if err := fsutil.Publish(dir, tmp.Name(), target); err != nil {
		return nil, err
	}
	keep = true
	if own {
		if err := f.DB.SetLocalPath(ctx, m.Conv, m.ID, target); err != nil {
			return nil, err
		}
	}
	res.Path = target
	return res, nil
}

// downloadSlack covers encryption padding and the MAC on top of the plain size.
const downloadSlack = 64 << 10

// limitedFile stops a download as soon as it writes past max bytes.
type limitedFile struct {
	*os.File
	max int64
}

func (l *limitedFile) Write(p []byte) (int, error) {
	if off, err := l.File.Seek(0, io.SeekCurrent); err == nil && off+int64(len(p)) > l.max {
		return 0, ErrTooLarge
	}
	return l.File.Write(p)
}

func (l *limitedFile) WriteAt(p []byte, off int64) (int, error) {
	if off+int64(len(p)) > l.max {
		return 0, ErrTooLarge
	}
	return l.File.WriteAt(p, off)
}

func (l *limitedFile) ReadFrom(r io.Reader) (int64, error) {
	return io.Copy(struct{ io.Writer }{l}, r)
}

func rewind(f *os.File) error {
	if err := f.Truncate(0); err != nil {
		return err
	}
	_, err := f.Seek(0, io.SeekStart)
	return err
}

// MediaType maps a stored kind to the whatsmeow key type.
func MediaType(kind string) whatsmeow.MediaType {
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

// FileName gives each message its own file: stickers are named after their
// content hash, documents keep their name plus a short message id (or only
// the id when private is set, so the file system does not show the name).
func FileName(m *store.Message, private bool) string {
	id := SafeName(m.ID)
	switch m.Kind {
	case store.KindSticker:
		if name := common.BaseName(m.AttachmentName); strings.HasPrefix(name, "sticker_") {
			return SafeName(name)
		}
		return "sticker_" + id + ".webp"
	case store.KindDocument:
		name := SafeName(common.BaseName(m.AttachmentName))
		ext := strings.ToLower(filepath.Ext(name))
		if ext == "" || len(ext) > 10 {
			ext = common.MediaExtension(m.Mime, ".bin")
		}
		if private || name == "_" || name == "" || strings.TrimSuffix(name, filepath.Ext(name)) == "" {
			return "document_" + id + ext
		}
		short := id
		if len(short) > 10 {
			short = short[:10]
		}
		return strings.TrimSuffix(name, filepath.Ext(name)) + "_" + short + filepath.Ext(name)
	case store.KindVoice, store.KindAudio:
		return m.Kind + "_" + id + common.MediaExtension(m.Mime, ".ogg")
	case store.KindVideo:
		return "video_" + id + common.MediaExtension(m.Mime, ".mp4")
	default:
		return m.Kind + "_" + id + common.MediaExtension(m.Mime, ".jpg")
	}
}

const maxNameRunes = 120

var reservedNames = map[string]bool{
	"CON": true, "PRN": true, "AUX": true, "NUL": true,
	"COM1": true, "COM2": true, "COM3": true, "COM4": true, "COM5": true, "COM6": true, "COM7": true, "COM8": true, "COM9": true,
	"LPT1": true, "LPT2": true, "LPT3": true, "LPT4": true, "LPT5": true, "LPT6": true, "LPT7": true, "LPT8": true, "LPT9": true,
}

// SafeName keeps Unicode letters and digits, space, dot, underscore and dash;
// everything else (separators, control and bidi characters, "@") becomes "_".
// The result has at most 120 runes, no leading or trailing dots or spaces and
// is never a reserved device name.
func SafeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r == utf8.RuneError:
			b.WriteRune('_')
		case unicode.IsLetter(r) || unicode.IsDigit(r) || r == ' ' || r == '.' || r == '_' || r == '-':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	out := strings.Trim(b.String(), " .")
	if utf8.RuneCountInString(out) > maxNameRunes {
		out = strings.Trim(string([]rune(out)[:maxNameRunes]), " .")
	}
	if out == "" {
		return "_"
	}
	stem, _, _ := strings.Cut(out, ".")
	if reservedNames[strings.ToUpper(strings.TrimRight(stem, " "))] {
		out = "_" + out
	}
	return out
}

// PictureSource returns a profile picture URL ("" when there is none).
type PictureSource interface {
	ProfilePicture(ctx context.Context, jid types.JID) (string, error)
}

// maxAvatarBytes caps profile picture downloads.
const maxAvatarBytes = 10 << 20

// SaveAvatar downloads the profile picture of jid into dir/avatars/<user>.jpg.
func SaveAvatar(ctx context.Context, src PictureSource, client *http.Client, dir string, jid types.JID) (string, error) {
	link, err := src.ProfilePicture(ctx, jid)
	if err != nil {
		return "", err
	}
	if link == "" {
		return "", ErrNoAvatar
	}
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, link, nil)
	if err != nil {
		return "", err
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", common.Upstream("profile picture download", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", common.Upstream("profile picture download", fmt.Errorf("HTTP %d", resp.StatusCode))
	}
	if resp.ContentLength > maxAvatarBytes {
		return "", ErrTooLarge
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxAvatarBytes+1))
	if err != nil {
		return "", common.Upstream("profile picture download", err)
	}
	if len(data) > maxAvatarBytes {
		return "", ErrTooLarge
	}
	target := filepath.Join(dir, "avatars", SafeName(jid.User)+".jpg")
	tmp, err := fsutil.CreateTemp(dir, ".part-*")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	_, writeErr := tmp.Write(data)
	if err := errors.Join(writeErr, tmp.Close()); err != nil {
		return "", err
	}
	if err := fsutil.Publish(dir, tmp.Name(), target); err != nil {
		return "", err
	}
	return target, nil
}
