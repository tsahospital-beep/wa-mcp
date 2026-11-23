package importer

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

const (
	aliceJID = "15550100002@s.whatsapp.net"
	meJID    = "15550100001@s.whatsapp.net"
	groupJID = "120363000000000001@g.us"
)

// exportText is a small Android export of a chat between Alice and Bob (Bob is the account owner).
const exportText = `13/01/2024, 09:00 - Alice: Good morning
13/01/2024, 09:01 - Bob: Morning
13/01/2024, 09:01 - Bob: Morning
13/01/2024, 09:05 - Alice: IMG-20240113-WA0001.jpg (file attached)
Lunch menu
13/01/2024, 09:06 - Bob: <Media omitted>
13/01/2024, 09:07 - Alice: PTT-20240113-WA0002.opus (file attached)
13/01/2024, 09:08 - Alice: DOC-20240113-WA0003.pdf (file attached)
14/01/2024, 18:30 - Alice: See you tomorrow
`

type fixture struct {
	t         *testing.T
	db        *store.DB
	downloads string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "store", "wa.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := db.SetAccountJID(context.Background(), meJID); err != nil {
		t.Fatal(err)
	}
	return &fixture{t: t, db: db, downloads: filepath.Join(dir, "downloads")}
}

func (f *fixture) opts(chat string) Options {
	return Options{Chat: chat, Me: "Bob", Location: time.UTC, DownloadsDir: f.downloads}
}

func (f *fixture) run(data []byte, name string, o Options) (*Report, error) {
	f.t.Helper()
	return Run(context.Background(), f.db, Source{R: bytes.NewReader(data), Size: int64(len(data)), Name: name}, o)
}

func (f *fixture) mustRun(data []byte, name string, o Options) *Report {
	f.t.Helper()
	rep, err := f.run(data, name, o)
	if err != nil {
		f.t.Fatalf("import: %v", err)
	}
	return rep
}

func (f *fixture) messages(chat string) []store.Message {
	f.t.Helper()
	list, err := f.db.ListMessages(context.Background(), store.MessageQuery{Conv: chat, Ascending: true, Limit: 1000})
	if err != nil {
		f.t.Fatal(err)
	}
	return list
}

type entry struct {
	name string
	data []byte
	mode os.FileMode
}

func makeZip(t *testing.T, entries ...entry) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for _, e := range entries {
		h := &zip.FileHeader{Name: e.name, Method: zip.Deflate}
		if e.mode != 0 {
			h.SetMode(e.mode)
		}
		w, err := zw.CreateHeader(h)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write(e.data); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func mediaZip(t *testing.T) []byte {
	return makeZip(t,
		entry{name: "WhatsApp Chat with Alice Example.txt", data: []byte(exportText)},
		entry{name: "IMG-20240113-WA0001.jpg", data: []byte("jpeg bytes")},
		entry{name: "PTT-20240113-WA0002.opus", data: []byte("opus bytes")},
		entry{name: "DOC-20240113-WA0003.pdf", data: []byte("pdf bytes")},
	)
}

func TestImportTextFile(t *testing.T) {
	f := newFixture(t)
	rep := f.mustRun([]byte(exportText), "WhatsApp Chat with Alice Example.txt", f.opts("15550100002"))
	if rep.Chat != aliceJID || !rep.NewChat || rep.MessagesAdded != 8 || rep.Duplicates != 0 {
		t.Fatalf("report %+v", rep)
	}
	if rep.MediaMissing != 3 || rep.MediaOmitted != 1 || rep.MediaAdded != 0 || rep.DateOrder != "dmy" || rep.Me != "Bob" {
		t.Errorf("media counts %+v", rep)
	}
	if rep.First != "2024-01-13T09:00:00Z" || rep.Last != "2024-01-14T18:30:00Z" {
		t.Errorf("range %s .. %s", rep.First, rep.Last)
	}
	list := f.messages(aliceJID)
	if len(list) != 8 {
		t.Fatalf("stored %d messages", len(list))
	}
	var bodies []string
	for _, m := range list {
		bodies = append(bodies, m.Body)
		if !strings.HasPrefix(m.ID, store.ImportedPrefix) {
			t.Errorf("id %s lacks the prefix", m.ID)
		}
		if m.Outgoing && (m.Author != meJID || m.ReadAt != 0) || !m.Outgoing && (m.Author != aliceJID || m.ReadAt != m.SentAt) {
			t.Errorf("author or read state wrong: %+v", m)
		}
	}
	if bodies[0] != "Good morning" || bodies[1] != "Morning" || bodies[2] != "Morning" || bodies[7] != "See you tomorrow" {
		t.Errorf("order or text wrong: %q", bodies)
	}
	if list[1].SentAt+1 != list[2].SentAt {
		t.Errorf("messages in one minute keep their order: %d %d", list[1].SentAt, list[2].SentAt)
	}
	img := list[3]
	if img.Kind != store.KindUnsupported || !strings.Contains(img.Body, "Lunch menu") || !strings.Contains(img.Body, "IMG-20240113-WA0001.jpg") {
		t.Errorf("attachment without a file: %+v", img)
	}
	if list[4].Kind != store.KindUnsupported || list[4].Body != "[media omitted]" {
		t.Errorf("omitted media: %+v", list[4])
	}
	conv, err := f.db.GetConversation(context.Background(), aliceJID)
	if err != nil || conv.Title != "Alice Example" || conv.IsGroup || conv.LastActivity != list[7].SentAt {
		t.Errorf("conversation %+v %v", conv, err)
	}
}

func TestReimportAddsNothing(t *testing.T) {
	f := newFixture(t)
	f.mustRun([]byte(exportText), "", f.opts(aliceJID))
	again := f.mustRun([]byte(exportText), "", f.opts(aliceJID))
	if again.MessagesAdded != 0 || again.Duplicates != 8 || again.AlreadyImported != 8 || again.MediaAdded != 0 || again.NewChat {
		t.Fatalf("second import %+v", again)
	}
	if n := len(f.messages(aliceJID)); n != 8 {
		t.Fatalf("%d rows after the second import", n)
	}
}

func TestDryRunWritesNothing(t *testing.T) {
	f := newFixture(t)
	rep := f.mustRun(mediaZip(t), "", func() Options { o := f.opts(aliceJID); o.DryRun = true; return o }())
	if !rep.DryRun || rep.MessagesAdded != 8 || rep.MediaAdded != 3 {
		t.Fatalf("report %+v", rep)
	}
	if n := len(f.messages(aliceJID)); n != 0 {
		t.Errorf("a dry run stored %d messages", n)
	}
	if _, err := f.db.GetConversation(context.Background(), aliceJID); !errors.Is(err, store.ErrNotFound) {
		t.Errorf("a dry run created the chat: %v", err)
	}
	if _, err := os.Stat(f.downloads); !os.IsNotExist(err) {
		t.Errorf("a dry run wrote files: %v", err)
	}
}

func TestZipWithMedia(t *testing.T) {
	f := newFixture(t)
	rep := f.mustRun(mediaZip(t), "WhatsApp Chat with Alice Example.zip", f.opts(aliceJID))
	if rep.MessagesAdded != 8 || rep.MediaAdded != 3 || rep.MediaMissing != 0 {
		t.Fatalf("report %+v", rep)
	}
	list := f.messages(aliceJID)
	want := map[int]struct{ kind, content string }{
		3: {store.KindImage, "jpeg bytes"}, 5: {store.KindVoice, "opus bytes"}, 6: {store.KindDocument, "pdf bytes"},
	}
	for i, w := range want {
		m := list[i]
		if m.Kind != w.kind || m.LocalPath == "" {
			t.Fatalf("row %d: %+v", i, m)
		}
		data, err := os.ReadFile(m.LocalPath)
		if err != nil || string(data) != w.content {
			t.Errorf("row %d file: %q %v", i, data, err)
		}
		if !strings.HasPrefix(m.LocalPath, filepath.Join(f.downloads, "15550100002_s.whatsapp.net")) {
			t.Errorf("file placed in %s", m.LocalPath)
		}
	}
	if list[3].Body != "Lunch menu" || list[3].AttachmentName != "IMG-20240113-WA0001.jpg" || list[3].Mime != "image/jpeg" || list[3].ByteSize != 10 {
		t.Errorf("image row %+v", list[3])
	}
	if leftovers, _ := filepath.Glob(filepath.Join(f.downloads, "*", ".part-*")); len(leftovers) > 0 {
		t.Errorf("temporary files left: %v", leftovers)
	}
	again := f.mustRun(mediaZip(t), "", f.opts(aliceJID))
	if again.MessagesAdded != 0 || again.MediaAdded != 0 {
		t.Errorf("second import %+v", again)
	}
}

func TestZipAfterTextFileLinksMedia(t *testing.T) {
	f := newFixture(t)
	f.mustRun([]byte(exportText), "", f.opts(aliceJID))
	rep := f.mustRun(mediaZip(t), "", f.opts(aliceJID))
	if rep.MessagesAdded != 0 || rep.AlreadyImported != 8 || rep.MediaAdded != 3 {
		t.Fatalf("report %+v", rep)
	}
	list := f.messages(aliceJID)
	if list[3].Kind != store.KindImage || list[3].LocalPath == "" || list[3].Body != "Lunch menu" {
		t.Errorf("the placeholder was not replaced: %+v", list[3])
	}
	if list[6].Kind != store.KindDocument || list[7].Kind != store.KindText {
		t.Errorf("kinds: %s %s", list[6].Kind, list[7].Kind)
	}
	again := f.mustRun(mediaZip(t), "", f.opts(aliceJID))
	if again.MediaAdded != 0 {
		t.Errorf("third import linked %d files again", again.MediaAdded)
	}
}

func TestDedupeAgainstSyncedMessages(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	at := func(s string) int64 {
		ts, err := time.Parse("2006-01-02 15:04:05", s)
		if err != nil {
			t.Fatal(err)
		}
		return ts.Unix()
	}
	for _, m := range []store.Message{
		{ID: "3EB0A1", Author: aliceJID, SentAt: at("2024-01-13 09:00:40"), Body: "Good morning"},
		{ID: "3EB0A2", Author: meJID, Outgoing: true, SentAt: at("2024-01-13 09:02:30"), Body: "Morning"},
		{ID: "3EB0A3", Author: aliceJID, SentAt: at("2024-01-14 18:34:00"), Body: "See you tomorrow"},
		{ID: "3EB0A4", Author: meJID, Outgoing: true, SentAt: at("2024-01-13 09:00:10"), Body: "Good morning"},
	} {
		m.Conv = aliceJID
		if err := f.db.PutMessage(ctx, &m); err != nil {
			t.Fatal(err)
		}
	}
	rep := f.mustRun([]byte(exportText), "", f.opts(aliceJID))
	if rep.AlreadySynced != 2 || rep.MessagesAdded != 6 || rep.Duplicates != 2 {
		t.Fatalf("report %+v", rep)
	}
	var good, morning, see int
	for _, m := range f.messages(aliceJID) {
		switch {
		case m.Body == "Good morning":
			good++
		case m.Body == "Morning":
			morning++
		case m.Body == "See you tomorrow":
			see++
		}
	}
	// Alice's "Good morning" and one of Bob's "Morning" were synced; the other "Morning" and the late
	// "See you tomorrow" (four minutes apart) are new; Bob's own "Good morning" is another message.
	if good != 2 || morning != 2 || see != 2 {
		t.Errorf("good %d morning %d see %d", good, morning, see)
	}
}

func TestDateOrderMustBeKnown(t *testing.T) {
	f := newFixture(t)
	text := "03/04/24, 10:00 - Alice: x\n04/04/24, 10:00 - Bob: y\n"
	if _, err := f.run([]byte(text), "", f.opts(aliceJID)); !errors.Is(err, ErrDateAmbiguous) {
		t.Fatalf("error %v", err)
	}
	if n := len(f.messages(aliceJID)); n != 0 {
		t.Fatalf("%d rows stored", n)
	}
	o := f.opts(aliceJID)
	o.DryRun = true
	rep := f.mustRun([]byte(text), "", o)
	if rep.DateOrderSource != SourceAssumed || len(rep.Warnings) == 0 {
		t.Errorf("dry run %+v", rep)
	}
	o = f.opts(aliceJID)
	o.DateOrder = "mdy"
	rep = f.mustRun([]byte(text), "", o)
	if rep.DateOrder != "mdy" || rep.First != "2024-03-04T10:00:00Z" {
		t.Errorf("forced order %+v", rep)
	}
}

func TestChatResolution(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	for _, c := range []store.Conversation{
		{JID: aliceJID, Title: "Alice Example"},
		{JID: "15550100003@s.whatsapp.net", Title: "Carol"},
		{JID: groupJID, Title: "Book club", IsGroup: true},
		{JID: "120363000000000002@g.us", Title: "Book club", IsGroup: true},
	} {
		if err := f.db.TouchConversation(ctx, c); err != nil {
			t.Fatal(err)
		}
	}
	conv, jid, err := resolveChat(ctx, f.db, "alice example")
	if err != nil || jid != aliceJID || conv == nil {
		t.Fatalf("title: %v %s", err, jid)
	}
	if conv, jid, err = resolveChat(ctx, f.db, "+1 (555) 010-0003"); err != nil || jid != "15550100003@s.whatsapp.net" || conv == nil {
		t.Fatalf("phone: %v %s", err, jid)
	}
	if conv, jid, err = resolveChat(ctx, f.db, "15550109999"); err != nil || conv != nil || jid != "15550109999@s.whatsapp.net" {
		t.Fatalf("unknown phone creates a chat: %v %v %s", err, conv, jid)
	}
	var ce *ChatError
	_, _, err = resolveChat(ctx, f.db, "Book club")
	if !errors.As(err, &ce) || !ce.Ambiguous || len(ce.Candidates) != 2 {
		t.Fatalf("ambiguous title: %v", err)
	}
	_, _, err = resolveChat(ctx, f.db, "Alice")
	if !errors.As(err, &ce) || ce.Ambiguous || len(ce.Candidates) != 1 || ce.Candidates[0].JID != aliceJID {
		t.Fatalf("near title should list candidates: %v %+v", err, ce)
	}
	if _, _, err = resolveChat(ctx, f.db, "   "); !errors.As(err, &ce) {
		t.Fatalf("empty chat: %v", err)
	}
	if _, err = f.run([]byte(exportText), "", f.opts("Nobody")); !errors.As(err, &ce) {
		t.Fatalf("import into an unknown title: %v", err)
	}
}

func TestMeDetection(t *testing.T) {
	f := newFixture(t)
	o := f.opts(aliceJID)
	o.Me = ""
	var me *MeError
	if _, err := f.run([]byte(exportText), "notes.txt", o); !errors.As(err, &me) || len(me.Senders) != 2 {
		t.Fatalf("no way to tell: %v", err)
	}
	if n := len(f.messages(aliceJID)); n != 0 {
		t.Fatalf("%d rows stored before me was known", n)
	}
	o.DryRun = true
	rep := f.mustRun([]byte(exportText), "notes.txt", o)
	if rep.Me != "" || len(rep.Warnings) == 0 || len(rep.Senders) != 2 {
		t.Errorf("dry run lists senders and warns: %+v", rep)
	}
	o.DryRun = false
	rep = f.mustRun([]byte(exportText), "WhatsApp Chat with Alice.txt", o)
	if rep.Me != "Bob" {
		t.Errorf("the file name names the other person: %+v", rep)
	}

	g := newFixture(t)
	o = g.opts(aliceJID)
	o.Me, o.PushName = "", "BOB"
	if rep = g.mustRun([]byte(exportText), "", o); rep.Me != "Bob" {
		t.Errorf("push name: %+v", rep)
	}
	o.Me = "Robert"
	if _, err := g.run([]byte(exportText), "", o); !errors.As(err, &me) {
		t.Errorf("a name that is not in the file: %v", err)
	}

	h := newFixture(t)
	text := "13/01/2024, 09:00 - +1 555 010 2222: hi\n13/01/2024, 09:01 - Bob: hello\n"
	o = h.opts("15550102222")
	o.Me = ""
	if rep = h.mustRun([]byte(text), "", o); rep.Me != "Bob" || rep.Senders[0].Author != "15550102222@s.whatsapp.net" {
		t.Errorf("phone number as the other name: %+v", rep)
	}
}

func TestGroupAuthors(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	if err := f.db.TouchConversation(ctx, store.Conversation{JID: aliceJID, Title: "Alice"}); err != nil {
		t.Fatal(err)
	}
	text := "13/01/2024, 09:00 - Alice: hi all\n13/01/2024, 09:01 - Bob: hello\n13/01/2024, 09:02 - +1 555 010 3333: hey\n" +
		"13/01/2024, 09:03 - Dave: yo\n13/01/2024, 09:04 - Erin: sup\n"
	o := f.opts(groupJID)
	o.Names = map[string]string{"erin": "15550104444"}
	rep := f.mustRun([]byte(text), "WhatsApp Chat - Book club.zip", o)
	got := map[string]SenderReport{}
	for _, s := range rep.Senders {
		got[s.Name] = s
	}
	if got["Alice"].Author != aliceJID || got["Bob"].Author != meJID || !got["Bob"].Outgoing ||
		got["+1 555 010 3333"].Author != "15550103333@s.whatsapp.net" || got["Dave"].Author != "name:Dave" ||
		got["Erin"].Author != "15550104444@s.whatsapp.net" {
		t.Fatalf("senders %+v", rep.Senders)
	}
	if len(rep.Warnings) != 1 || !strings.Contains(rep.Warnings[0], "Dave") {
		t.Errorf("warnings %v", rep.Warnings)
	}
	conv, err := f.db.GetConversation(ctx, groupJID)
	if err != nil || !conv.IsGroup || conv.Title != "Book club" {
		t.Errorf("group chat %+v %v", conv, err)
	}
	o.Names = map[string]string{"Erin": "not a number"}
	if _, err := f.run([]byte(text), "", o); err == nil {
		t.Error("a bad JID in names must be refused")
	}
}

func TestEncryptedStore(t *testing.T) {
	f := newFixture(t)
	if err := f.db.SetupEncryption(context.Background(), true, "correct horse"); err != nil {
		t.Fatal(err)
	}
	f.mustRun([]byte(exportText), "", f.opts(aliceJID))
	list := f.messages(aliceJID)
	if len(list) != 8 || list[0].Body != "Good morning" {
		t.Fatalf("rows %d", len(list))
	}
	var raw string
	if err := f.db.SQL().QueryRow(`SELECT body FROM chat_messages WHERE msg_id = ?`, list[0].ID).Scan(&raw); err != nil || raw == "Good morning" {
		t.Errorf("body is stored in plain form: %q %v", raw, err)
	}
}

func TestNoMessagesInFile(t *testing.T) {
	f := newFixture(t)
	text := "13/01/2024, 09:00 - Messages and calls are end-to-end encrypted.\n13/01/2024, 09:01 - Alice added Bob\n"
	if _, err := f.run([]byte(text), "", f.opts(aliceJID)); err == nil {
		t.Fatal("a file with only system lines must be refused")
	}
}

func TestMessageIDIsStable(t *testing.T) {
	a := messageID(aliceJID, 100, "Alice", "hi", 0)
	if a != messageID(aliceJID, 100, "Alice", "hi", 0) || !strings.HasPrefix(a, "imp-") || len(a) != 4+24 {
		t.Fatalf("id %s", a)
	}
	for _, other := range []string{
		messageID("15550100003@s.whatsapp.net", 100, "Alice", "hi", 0), messageID(aliceJID, 101, "Alice", "hi", 0),
		messageID(aliceJID, 100, "Bob", "hi", 0), messageID(aliceJID, 100, "Alice", "ho", 0), messageID(aliceJID, 100, "Alice", "hi", 1),
	} {
		if other == a {
			t.Error("different inputs gave the same id")
		}
	}
}
