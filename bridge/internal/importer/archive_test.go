package importer

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

var chatEntry = entry{name: "_chat.txt", data: []byte("13/01/2024, 09:00 - Alice: hi\n13/01/2024, 09:01 - Bob: hello\n")}

func TestUnsafeEntriesRefused(t *testing.T) {
	cases := map[string]entry{
		"parent folder":    {name: "../evil.txt", data: []byte("x")},
		"nested parent":    {name: "media/../../evil.txt", data: []byte("x")},
		"backslash parent": {name: "..\\evil.txt", data: []byte("x")},
		"absolute":         {name: "/etc/evil.txt", data: []byte("x")},
		"drive letter":     {name: "C:\\evil.txt", data: []byte("x")},
		"alternate stream": {name: "notes.txt:hidden", data: []byte("x")},
		"unc path":         {name: "\\\\server\\share\\evil.txt", data: []byte("x")},
		"symbolic link":    {name: "link.jpg", data: []byte("/etc/passwd"), mode: os.ModeSymlink | 0o777},
		"nul in the name":  {name: "a\x00b.txt", data: []byte("x")},
	}
	for name, e := range cases {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			outside := filepath.Dir(f.downloads)
			data := makeZip(t, chatEntry, e)
			_, err := f.run(data, "", f.opts(aliceJID))
			if !errors.Is(err, ErrUnsafeArchive) {
				t.Fatalf("error %v, want ErrUnsafeArchive", err)
			}
			if n := len(f.messages(aliceJID)); n != 0 {
				t.Errorf("%d messages stored from a refused archive", n)
			}
			assertNoFile(t, outside, "evil.txt")
			assertNoFile(t, filepath.Dir(outside), "evil.txt")
		})
	}
}

func assertNoFile(t *testing.T, root, base string) {
	t.Helper()
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.Name() == base {
			t.Errorf("unexpected file %s", p)
		}
		return nil
	})
}

func TestFolderInsideArchiveIsFine(t *testing.T) {
	f := newFixture(t)
	text := "13/01/2024, 09:00 - Alice: IMG-1.jpg (file attached)\n13/01/2024, 09:01 - Bob: hello\n"
	data := makeZip(t,
		entry{name: "export/", data: nil},
		entry{name: "export/WhatsApp Chat with Alice.txt", data: []byte(text)},
		entry{name: "export/Media/IMG-1.jpg", data: []byte("jpeg")},
	)
	rep := f.mustRun(data, "", f.opts(aliceJID))
	if rep.MessagesAdded != 2 || rep.MediaAdded != 1 {
		t.Fatalf("report %+v", rep)
	}
	list := f.messages(aliceJID)
	if want := filepath.Join(f.downloads, "15550100002_s.whatsapp.net", "image_"+list[0].ID+".jpg"); list[0].LocalPath != want {
		t.Errorf("the file name comes from the message id, not from the archive: %s", list[0].LocalPath)
	}
}

func TestChatFileChoice(t *testing.T) {
	f := newFixture(t)
	data := makeZip(t,
		entry{name: "readme.txt", data: []byte("not a chat, but the largest file in this archive, by far, long long long long long long long long")},
		entry{name: "WhatsApp Chat with Alice.txt", data: []byte("13/01/2024, 09:00 - Alice: right one\n13/01/2024, 09:01 - Bob: yes\n")},
	)
	if rep := f.mustRun(data, "", f.opts(aliceJID)); rep.MessagesAdded != 2 {
		t.Fatalf("report %+v", rep)
	}
	if _, err := f.run(makeZip(t, entry{name: "photo.jpg", data: []byte("x")}), "", f.opts(aliceJID)); err == nil {
		t.Error("an archive without a text file must be refused")
	}
	if _, err := f.run([]byte("PK\x03\x04 broken"), "", f.opts(aliceJID)); err == nil {
		t.Error("a damaged archive must be refused")
	}
}

func TestZipBombRefused(t *testing.T) {
	f := newFixture(t)
	zeros := make([]byte, 24<<20)
	data := makeZip(t, chatEntry, entry{name: "IMG-9.jpg", data: zeros})
	if len(data) > 1<<20 {
		t.Fatalf("test archive is %d bytes; it should compress far below 1 MiB", len(data))
	}
	_, err := f.run(data, "", f.opts(aliceJID))
	if !errors.Is(err, ErrUnsafeArchive) || !strings.Contains(err.Error(), "zip bomb") {
		t.Fatalf("error %v", err)
	}
	if n := len(f.messages(aliceJID)); n != 0 {
		t.Errorf("%d messages stored", n)
	}
}

func TestArchiveLimits(t *testing.T) {
	f := newFixture(t)
	o := f.opts(aliceJID)

	o.Limits = Limits{MaxBytes: 1 << 20}
	big := makeZip(t, chatEntry, entry{name: "a.bin", data: pseudoRandom(2 << 20)})
	if _, err := f.run(big, "", o); !errors.Is(err, ErrTooLarge) {
		t.Errorf("unpacked size: %v", err)
	}

	o.Limits = Limits{MaxEntries: 3}
	many := makeZip(t, chatEntry, entry{name: "a", data: []byte("1")}, entry{name: "b", data: []byte("1")}, entry{name: "c", data: []byte("1")})
	if _, err := f.run(many, "", o); !errors.Is(err, ErrTooLarge) {
		t.Errorf("entry count: %v", err)
	}

	o.Limits = Limits{MaxBytes: 1 << 20}
	text := bytes.Repeat([]byte("13/01/2024, 09:00 - Alice: filler filler filler\n"), 40000)
	if _, err := f.run(text, "", o); !errors.Is(err, ErrTooLarge) {
		t.Errorf("a text file above the limit: %v", err)
	}
	if n := len(f.messages(aliceJID)); n != 0 {
		t.Errorf("%d messages stored", n)
	}
}

func pseudoRandom(n int) []byte {
	b := make([]byte, n)
	x := uint32(2463534242)
	for i := range b {
		x ^= x << 13
		x ^= x >> 17
		x ^= x << 5
		b[i] = byte(x)
	}
	return b
}

func TestAttachmentLimitsKeepTheMessage(t *testing.T) {
	f := newFixture(t)
	text := "13/01/2024, 09:00 - Alice: BIG-1.mp4 (file attached)\n13/01/2024, 09:01 - Alice: ZERO-2.jpg (file attached)\n" +
		"13/01/2024, 09:02 - Alice: OK-3.jpg (file attached)\n13/01/2024, 09:03 - Bob: fine\n"
	data := makeZip(t,
		entry{name: "_chat.txt", data: []byte(text)},
		entry{name: "BIG-1.mp4", data: pseudoRandom(3 << 20)},
		entry{name: "ZERO-2.jpg", data: make([]byte, 4<<20)},
		entry{name: "OK-3.jpg", data: []byte("small")},
	)
	o := f.opts(aliceJID)
	o.Limits = Limits{MaxMediaBytes: 2 << 20}
	rep := f.mustRun(data, "", o)
	if rep.MessagesAdded != 4 || rep.MediaAdded != 1 || rep.MediaSkipped != 2 || len(rep.Warnings) != 2 {
		t.Fatalf("report %+v", rep)
	}
	list := f.messages(aliceJID)
	if list[0].Kind != store.KindUnsupported || list[0].LocalPath != "" || !strings.Contains(list[0].Body, "BIG-1.mp4") {
		t.Errorf("too large: %+v", list[0])
	}
	if list[2].LocalPath == "" {
		t.Errorf("the small file was not kept: %+v", list[2])
	}
	files, _ := filepath.Glob(filepath.Join(f.downloads, "*", "*"))
	if len(files) != 1 {
		t.Errorf("files on disk: %v", files)
	}
}

func TestCopyEntryStopsAtTheCap(t *testing.T) {
	dir := t.TempDir()
	data := makeZip(t, entry{name: "a.bin", data: pseudoRandom(100)})
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	w := &work{ctx: context.Background()}
	target := filepath.Join(dir, "out.bin")
	n, reason, err := w.copyEntry(zr.File[0], dir, target, 50)
	if err != nil || reason == "" || n != 0 {
		t.Fatalf("a copy above the cap must stop: %d %q %v", n, reason, err)
	}
	if left, _ := filepath.Glob(filepath.Join(dir, "*")); len(left) != 0 {
		t.Errorf("files left behind: %v", left)
	}
	if n, reason, err = w.copyEntry(zr.File[0], dir, target, 100); err != nil || reason != "" || n != 100 {
		t.Fatalf("a copy at the cap works: %d %q %v", n, reason, err)
	}
}

func TestOpenInput(t *testing.T) {
	dir := t.TempDir()
	allowed := filepath.Join(dir, "imports")
	storeDir := filepath.Join(dir, "store")
	for _, d := range []string{allowed, storeDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	write := func(p string) string {
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	good := write(filepath.Join(allowed, "chat.txt"))
	outside := write(filepath.Join(dir, "outside.txt"))
	secret := write(filepath.Join(storeDir, "wa.db"))
	cfg := write(filepath.Join(dir, "config.json"))
	denied := []string{storeDir, cfg}

	f, err := OpenInput(good, []string{allowed}, denied)
	if err != nil {
		t.Fatalf("allowed file: %v", err)
	}
	f.Close()

	var ie *InputError
	for name, c := range map[string]struct {
		path    string
		allowed []string
		forbid  bool
	}{
		"outside the folder": {outside, []string{allowed}, true},
		"dot dot escape":     {filepath.Join(allowed, "..", "outside.txt"), []string{allowed}, true},
		"store file":         {secret, []string{dir}, true},
		"config file":        {cfg, []string{dir}, true},
		"folder":             {allowed, []string{dir}, false},
		"missing":            {filepath.Join(allowed, "none.txt"), []string{allowed}, false},
		"empty":              {"", []string{allowed}, false},
	} {
		if _, err := OpenInput(c.path, c.allowed, denied); !errors.As(err, &ie) || ie.Forbidden != c.forbid {
			t.Errorf("%s: %v", name, err)
		}
	}

	link := filepath.Join(allowed, "link.txt")
	if err := os.Symlink(outside, link); err == nil {
		if _, err := OpenInput(link, []string{allowed}, denied); err == nil {
			t.Error("a symbolic link to a file outside must be refused")
		}
	}
	hard := filepath.Join(allowed, "hard.txt")
	if err := os.Link(secret, hard); err == nil {
		if _, err := OpenInput(hard, []string{allowed}, denied); err == nil {
			t.Error("a hard link to a store file must be refused")
		}
	}
	if f, err := OpenInput(outside, nil, denied); err != nil {
		t.Errorf("no allowed list means any folder: %v", err)
	} else {
		f.Close()
	}
}
