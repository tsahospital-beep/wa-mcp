package importer

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// work holds the state of one import after the file was read.
type work struct {
	ctx     context.Context
	db      *store.DB
	o       Options
	lim     Limits
	ar      *archive
	rep     *Report
	jid     string
	loc     *time.Location
	parsed  *Parsed
	senders map[string]SenderReport
	budget  int64 // bytes of attachments that may still be written
}

// synced is the key of a stored message an exported one is compared with.
type synced struct {
	outgoing bool
	body     string
}

// item is a message that will be stored, with the archive entry of its file.
type item struct {
	row  *store.Message
	file *zip.File
	keys []string // dedupe keys, the first that matches decides
}

func normBody(s string) string { return strings.Join(strings.Fields(s), " ") }

func storedKey(m *store.Message) string {
	if b := normBody(m.Body); b != "" {
		return b
	}
	return "kind:" + m.Kind
}

// messageID is the same for the same message in every import of the same file: it depends on the
// chat, the minute, the sender name as written and the text, and on how often that combination
// already occurred earlier in the file.
func messageID(chat string, minute int64, sender, raw string, n int) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s\x00%d\x00%s\x00%s", chat, minute, sender, raw)
	if n > 0 {
		fmt.Fprintf(h, "\x00%d", n)
	}
	return store.ImportedPrefix + hex.EncodeToString(h.Sum(nil))[:24]
}

func (w *work) run(title string, group bool) error {
	msgs := w.parsed.Messages
	first, last := msgs[0].At, msgs[0].At
	for _, m := range msgs[1:] {
		if m.At.Before(first) {
			first = m.At
		}
		if m.At.After(last) {
			last = m.At
		}
	}
	w.rep.First, w.rep.Last = first.Format(time.RFC3339), last.Format(time.RFC3339)
	w.budget = w.lim.MaxBytes

	// Rows near the imported range: earlier imports (by id) and synced messages (by content and time).
	imported := map[string]bool{} // id -> has a file
	stored := map[synced][]int64{}
	slack := int64(dedupeWindow / time.Second)
	err := w.db.ScanMessages(w.ctx, w.jid, first.Unix()-slack, last.Unix()+slack, func(m *store.Message) {
		if strings.HasPrefix(m.ID, store.ImportedPrefix) {
			imported[m.ID] = m.LocalPath != ""
			return
		}
		k := synced{m.Outgoing, storedKey(m)}
		stored[k] = append(stored[k], m.SentAt)
	})
	if err != nil {
		return err
	}

	seen := map[string]int{}
	var add, upgrade []*item
	for _, m := range msgs {
		if err := w.ctx.Err(); err != nil {
			return err
		}
		minute := m.At.Unix() / 60
		key := fmt.Sprintf("%d\x00%s\x00%s", minute, m.Sender, m.Raw)
		n := seen[key]
		seen[key]++
		it := w.build(m, messageID(w.jid, minute, m.Sender, m.Raw, n))
		if hasFile, ok := imported[it.row.ID]; ok {
			w.rep.AlreadyImported++
			if !hasFile && it.file != nil {
				upgrade = append(upgrade, it)
			}
			continue
		}
		if matchSynced(stored, it) {
			w.rep.AlreadySynced++
			continue
		}
		add = append(add, it)
	}
	w.rep.Duplicates = w.rep.AlreadyImported + w.rep.AlreadySynced

	rows := make([]*store.Message, len(add))
	for i, it := range add {
		placed, err := w.attach(it)
		if err != nil {
			return err
		}
		if placed {
			w.rep.MediaAdded++
		}
		rows[i] = it.row
	}
	var n int
	if w.o.DryRun {
		n = len(rows)
	} else if n, err = w.db.InsertMessages(w.ctx, rows, 500); err != nil {
		return err
	}
	w.rep.MessagesAdded = n
	for _, it := range upgrade {
		placed, err := w.attach(it)
		if err != nil {
			return err
		}
		if !placed {
			continue
		}
		linked := true
		if !w.o.DryRun {
			if linked, err = w.db.LinkImportedFile(w.ctx, it.row); err != nil {
				return err
			}
		}
		if linked {
			w.rep.MediaAdded++
		}
	}
	if w.o.DryRun {
		return nil
	}
	if n > 0 {
		return w.db.TouchConversation(w.ctx, store.Conversation{JID: w.jid, Title: title, IsGroup: group, LastActivity: last.Unix()})
	}
	return nil
}

// matchSynced takes one stored message that is the same as the exported one: same direction, same
// text (or same kind when there is no text) and a time within the dedupe window. A stored message
// can be taken once.
func matchSynced(stored map[synced][]int64, it *item) bool {
	at := it.row.SentAt
	window := int64(dedupeWindow / time.Second)
	for _, key := range it.keys {
		k := synced{it.row.Outgoing, key}
		for i, t := range stored[k] {
			if t >= at-window && t <= at+window {
				stored[k] = append(stored[k][:i:i], stored[k][i+1:]...)
				return true
			}
		}
	}
	return false
}

var mediaKinds = []string{store.KindImage, store.KindVideo, store.KindAudio, store.KindVoice, store.KindDocument, store.KindSticker}

// build makes the row for one message. Attachments are not copied yet.
func (w *work) build(m Msg, id string) *item {
	s := w.senders[m.Sender]
	row := &store.Message{
		Conv: w.jid, ID: id, Author: s.Author, Outgoing: s.Outgoing, SentAt: m.At.Unix(),
		Kind: store.KindText, Body: m.Text,
	}
	if !row.Outgoing {
		row.ReadAt = row.SentAt
	}
	it := &item{row: row}
	var keys []string
	switch {
	case m.Attach != "":
		kind, mime := fileKind(m.Attach)
		row.AttachmentName, row.Mime = m.Attach, mime
		keys = kindKeys(m.Text, kind)
		if f := w.ar.lookup(m.Attach); f != nil {
			row.Kind, it.file = kind, f
			row.ByteSize = int64(f.UncompressedSize64)
		} else {
			w.rep.MediaMissing++
			w.degrade(row, kind+" not in the export", m.Attach)
		}
	case m.Omitted != "":
		w.rep.MediaOmitted++
		if m.Omitted == "media" {
			keys = kindKeys(m.Text, mediaKinds...)
		} else {
			keys = kindKeys(m.Text, m.Omitted)
		}
		w.degrade(row, m.Omitted+" omitted", "")
	default:
		keys = kindKeys(m.Text)
	}
	it.keys = keys
	return it
}

// kindKeys lists how a stored copy of the message may look: its text, or its kind when it has none.
func kindKeys(text string, kinds ...string) []string {
	if b := normBody(text); b != "" {
		return []string{b}
	}
	keys := make([]string, 0, len(kinds))
	for _, k := range kinds {
		keys = append(keys, "kind:"+k)
	}
	return keys
}

// degrade turns a media message without a file into a text placeholder of an unsupported kind.
func (w *work) degrade(row *store.Message, what, name string) {
	label := "[" + what
	if name != "" {
		label += ": " + name
	}
	label += "]"
	row.Kind = store.KindUnsupported
	row.LocalPath, row.ByteSize = "", 0
	if row.Body != "" {
		row.Body += "\n"
	}
	row.Body += label
}

func (a *archive) lookup(name string) *zip.File {
	if a == nil {
		return nil
	}
	return a.files[strings.ToLower(name)]
}

// attach places the file of an item in the downloads folder (or only checks it, in a dry run) and
// sets local_path. A file that cannot be used turns the message into a placeholder.
func (w *work) attach(it *item) (bool, error) {
	if it.file == nil {
		return false, nil
	}
	row := it.row
	reason, err := w.place(row, it.file)
	if err != nil {
		return false, err
	}
	if reason != "" {
		w.rep.MediaSkipped++
		w.rep.warn("attachment %q was not imported: %s", row.AttachmentName, reason)
		w.degrade(row, it.kindName()+" not imported", row.AttachmentName)
		it.file = nil
		return false, nil
	}
	return true, nil
}

func (it *item) kindName() string {
	k, _ := fileKind(it.row.AttachmentName)
	return k
}

// localName is the file name inside the chat folder: documents keep their name (see media.FileName),
// everything else is named after the kind and the message id with the original extension.
func localName(m *store.Message, private bool) string {
	if m.Kind == store.KindDocument {
		return media.FileName(m, private)
	}
	ext := strings.ToLower(path.Ext(m.AttachmentName))
	if len(ext) < 2 || len(ext) > 10 {
		ext = ".bin"
	}
	return m.Kind + "_" + media.SafeName(m.ID) + "." + media.SafeName(strings.TrimPrefix(ext, "."))
}

// place writes one attachment. It returns a reason when the entry is not usable (the message is
// kept without the file) and an error for a failure of the disk or the context.
func (w *work) place(row *store.Message, f *zip.File) (string, error) {
	size := int64(f.UncompressedSize64)
	switch {
	case f.Flags&0x1 != 0:
		return "the entry is password protected", nil
	case size > w.lim.MaxMediaBytes:
		return fmt.Sprintf("it is larger than %d MiB (media.max_download_mb)", w.lim.MaxMediaBytes>>20), nil
	}
	if err := ratioOK(f, w.lim); err != nil {
		return "it unpacks to far more than its stored size", nil
	}
	dir := filepath.Join(w.o.DownloadsDir, media.SafeName(w.jid))
	target := filepath.Join(dir, localName(row, w.o.Private))
	if sameEntry(target, f, w.o.DownloadsDir) {
		row.LocalPath = target
		return "", nil
	}
	if size > w.budget {
		return "the import size limit (import.max_mb) is used up", nil
	}
	if w.o.DryRun {
		w.budget -= size
		row.LocalPath = target
		return "", nil
	}
	n, reason, err := w.copyEntry(f, w.o.DownloadsDir, target, min(w.lim.MaxMediaBytes, w.budget))
	if err != nil || reason != "" {
		return reason, err
	}
	w.budget -= n
	row.LocalPath = target
	return "", nil
}

// copyEntry streams an entry into a private temporary file and renames it. More bytes than max
// stop the copy whatever the header says.
func (w *work) copyEntry(f *zip.File, dir, target string, max int64) (int64, string, error) {
	if err := w.ctx.Err(); err != nil {
		return 0, "", err
	}
	rc, err := f.Open()
	if err != nil {
		return 0, "the entry cannot be read: " + err.Error(), nil
	}
	defer rc.Close()
	tmp, err := fsutil.CreateTemp(dir, ".part-*")
	if err != nil {
		return 0, "", err
	}
	keep := false
	defer func() {
		tmp.Close()
		if !keep {
			fsutil.Wipe(tmp.Name())
		}
	}()
	n, err := io.Copy(tmp, io.LimitReader(rc, max+1))
	switch {
	case err != nil && !errors.Is(err, io.EOF):
		return 0, "the entry cannot be read: " + err.Error(), nil
	case n > max:
		return 0, "it unpacks to more than its header says or more than the limit", nil
	}
	if err := tmp.Close(); err != nil {
		return 0, "", err
	}
	if err := fsutil.Publish(dir, tmp.Name(), target); err != nil {
		return 0, "", err
	}
	keep = true
	return n, "", nil
}
