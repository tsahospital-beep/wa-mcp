package api

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/media"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

const importChat = `13/01/2024, 09:00 - Alice: Good morning
13/01/2024, 09:01 - Bob: Morning
13/01/2024, 09:05 - Alice: IMG-20240113-WA0001.jpg (file attached)
Lunch menu
13/01/2024, 09:06 - Bob: <Media omitted>
14/01/2024, 18:30 - Alice: See you tomorrow
`

const importAlice = "15550100002@s.whatsapp.net"

// pushBridge is a bridge that knows the push name of the account.
type pushBridge struct{ fakeBridge }

func (*pushBridge) PushName() string { return "Bob" }

type importEnv struct {
	srv  *httptest.Server
	cfg  *config.Config
	db   *store.DB
	root string
}

func newImportEnv(t *testing.T) *importEnv {
	t.Helper()
	dir := t.TempDir()
	cfg := config.Default()
	cfg.Dir = dir
	cfg.StoreDir = filepath.Join(dir, "store")
	cfg.DownloadsDir = filepath.Join(dir, "downloads")
	cfg.ExportsDir = filepath.Join(dir, "exports")
	cfg.Import.AllowedDirs = []string{filepath.Join(dir, "inbox")}
	cfg.Sending.DeniedPaths = []string{cfg.StoreDir}
	for _, d := range []string{cfg.DownloadsDir, cfg.ExportsDir, cfg.Import.AllowedDirs[0]} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	db, err := store.Open(cfg.MessagesDB())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := db.SetAccountJID(context.Background(), "15550100001@s.whatsapp.net"); err != nil {
		t.Fatal(err)
	}
	s := &Server{
		Cfg: cfg, DB: db, WA: &pushBridge{}, Media: fakeMedia{}, Token: token, Version: "test",
		Avatar: func(ctx context.Context, jid types.JID) (string, error) { return "", media.ErrNoAvatar },
	}
	srv := httptest.NewUnstartedServer(nil)
	srv.Config.Handler = s.Handler(srv.Listener.Addr().String())
	srv.Start()
	t.Cleanup(srv.Close)
	return &importEnv{srv: srv, cfg: cfg, db: db, root: dir}
}

func zipBytes(t *testing.T, files map[string][]byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for _, name := range []string{"WhatsApp Chat with Alice.txt", "IMG-20240113-WA0001.jpg", "../evil.txt", "big.bin"} {
		data, ok := files[name]
		if !ok {
			continue
		}
		w, err := zw.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Deflate})
		if err != nil {
			t.Fatal(err)
		}
		w.Write(data)
	}
	zw.Close()
	return buf.Bytes()
}

// upload posts a multipart form with the file and the fields.
func (e *importEnv) upload(t *testing.T, fileName string, data []byte, fields map[string]string) (int, map[string]any) {
	t.Helper()
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	for k, v := range fields {
		mw.WriteField(k, v)
	}
	if data != nil {
		fw, err := mw.CreateFormFile("file", fileName)
		if err != nil {
			t.Fatal(err)
		}
		fw.Write(data)
	}
	mw.Close()
	req, _ := http.NewRequest("POST", e.srv.URL+"/api/v1/import", &body)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	return e.send(t, req)
}

func (e *importEnv) postJSON(t *testing.T, payload any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(payload)
	req, _ := http.NewRequest("POST", e.srv.URL+"/api/v1/import", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	return e.send(t, req)
}

func (e *importEnv) send(t *testing.T, req *http.Request) (int, map[string]any) {
	t.Helper()
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := e.srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func (e *importEnv) count(t *testing.T, chat string) int {
	t.Helper()
	list, err := e.db.ListMessages(context.Background(), store.MessageQuery{Conv: chat, Limit: 1000})
	if err != nil {
		t.Fatal(err)
	}
	return len(list)
}

func TestImportUploadZipWithMedia(t *testing.T) {
	e := newImportEnv(t)
	data := zipBytes(t, map[string][]byte{"WhatsApp Chat with Alice.txt": []byte(importChat), "IMG-20240113-WA0001.jpg": []byte("jpeg bytes")})
	code, out := e.upload(t, "WhatsApp Chat with Alice.zip", data, map[string]string{"chat": "15550100002", "timezone": "UTC"})
	if code != 200 || out["ok"] != true {
		t.Fatalf("%d %v", code, out)
	}
	if out["messages_added"] != 5.0 || out["media_added"] != 1.0 || out["duplicates"] != 0.0 || out["system_lines"] != 0.0 ||
		out["date_order"] != "dmy" || out["me"] != "Bob" || out["chat"] != importAlice || out["dry_run"] != false {
		t.Fatalf("report %v", out)
	}
	skipped, _ := out["skipped_lines"].(map[string]any)
	if skipped["count"] != 0.0 {
		t.Errorf("skipped %v", skipped)
	}
	if out["first"] != "2024-01-13T09:00:00Z" || out["last"] != "2024-01-14T18:30:00Z" {
		t.Errorf("range %v %v", out["first"], out["last"])
	}
	list, _ := e.db.ListMessages(context.Background(), store.MessageQuery{Conv: importAlice, Ascending: true, Limit: 100})
	if len(list) != 5 || list[2].Kind != store.KindImage || list[2].LocalPath == "" || list[2].Body != "Lunch menu" {
		t.Fatalf("rows %+v", list)
	}
	if data, err := os.ReadFile(list[2].LocalPath); err != nil || string(data) != "jpeg bytes" {
		t.Errorf("file %q %v", data, err)
	}
	if !strings.HasPrefix(list[2].LocalPath, e.cfg.DownloadsDir) {
		t.Errorf("file outside downloads_dir: %s", list[2].LocalPath)
	}
	if left, _ := filepath.Glob(filepath.Join(e.cfg.StoreDir, "import-tmp", "*")); len(left) != 0 {
		t.Errorf("temporary upload left behind: %v", left)
	}

	code, out = e.upload(t, "WhatsApp Chat with Alice.zip", data, map[string]string{"chat": importAlice, "timezone": "UTC"})
	if code != 200 || out["messages_added"] != 0.0 || out["duplicates"] != 5.0 || out["media_added"] != 0.0 {
		t.Fatalf("second import %d %v", code, out)
	}
}

func TestImportDryRunAndTimezone(t *testing.T) {
	e := newImportEnv(t)
	code, out := e.upload(t, "chat.txt", []byte(importChat), map[string]string{
		"chat": "15550100002", "dry_run": "true", "timezone": "Europe/Berlin", "me": "bob",
	})
	if code != 200 || out["dry_run"] != true || out["messages_added"] != 5.0 || out["timezone"] != "Europe/Berlin" {
		t.Fatalf("%d %v", code, out)
	}
	if out["first"] != "2024-01-13T09:00:00+01:00" {
		t.Errorf("zone not applied: %v", out["first"])
	}
	if n := e.count(t, importAlice); n != 0 {
		t.Errorf("a dry run stored %d rows", n)
	}
	code, out = e.upload(t, "chat.txt", []byte(importChat), map[string]string{"chat": "15550100002", "timezone": "Nowhere/Land"})
	if code != 400 || !strings.Contains(out["error"].(string), "timezone") {
		t.Errorf("bad zone: %d %v", code, out)
	}
}

func TestImportByPath(t *testing.T) {
	e := newImportEnv(t)
	inbox := e.cfg.Import.AllowedDirs[0]
	good := filepath.Join(inbox, "chat.txt")
	os.WriteFile(good, []byte(importChat), 0o600)
	outside := filepath.Join(e.root, "outside.txt")
	os.WriteFile(outside, []byte(importChat), 0o600)
	inStore := filepath.Join(e.cfg.StoreDir, "notes.txt")
	os.MkdirAll(e.cfg.StoreDir, 0o755)
	os.WriteFile(inStore, []byte(importChat), 0o600)
	inDownloads := filepath.Join(e.cfg.DownloadsDir, "chat.txt")
	os.WriteFile(inDownloads, []byte(importChat), 0o600)

	body := func(path string) map[string]any {
		return map[string]any{"path": path, "chat": "15550100002", "me": "Bob", "timezone": "UTC", "date_order": "dmy"}
	}
	for name, c := range map[string]struct {
		path string
		code int
	}{
		"outside":   {outside, 403},
		"escape":    {filepath.Join(inbox, "..", "outside.txt"), 403},
		"store":     {inStore, 403},
		"missing":   {filepath.Join(inbox, "none.txt"), 404},
		"folder":    {inbox, 400},
		"empty":     {"", 400},
		"inbox":     {good, 200},
		"downloads": {inDownloads, 200},
	} {
		code, out := e.postJSON(t, body(c.path))
		if code != c.code {
			t.Errorf("%s: %d %v", name, code, out)
		}
	}
	if n := e.count(t, importAlice); n != 5 {
		t.Errorf("stored %d rows", n)
	}
}

func TestImportErrorsNameTheFix(t *testing.T) {
	e := newImportEnv(t)
	ctx := context.Background()
	for _, c := range []store.Conversation{
		{JID: importAlice, Title: "Alice"},
		{JID: "120363000000000001@g.us", Title: "Book club", IsGroup: true},
		{JID: "120363000000000002@g.us", Title: "Book club", IsGroup: true},
	} {
		e.db.TouchConversation(ctx, c)
	}
	form := func(chat, me string) map[string]string {
		return map[string]string{"chat": chat, "me": me, "timezone": "UTC"}
	}
	code, out := e.upload(t, "c.txt", []byte(importChat), form("Book club", "Bob"))
	cands, _ := out["candidates"].([]any)
	if code != 409 || len(cands) != 2 {
		t.Errorf("ambiguous title: %d %v", code, out)
	}
	code, out = e.upload(t, "c.txt", []byte(importChat), form("Alic", "Bob"))
	cands, _ = out["candidates"].([]any)
	if code != 404 || len(cands) != 1 {
		t.Errorf("unknown title: %d %v", code, out)
	}
	code, out = e.upload(t, "c.txt", []byte(importChat), form("", "Bob"))
	if code != 404 {
		t.Errorf("no chat: %d %v", code, out)
	}
	code, out = e.upload(t, "c.txt", []byte(importChat), form("Alice", "Robert"))
	senders, _ := out["senders"].([]any)
	if code != 400 || len(senders) != 2 {
		t.Errorf("wrong me: %d %v", code, out)
	}
	code, out = e.upload(t, "c.txt", []byte("hello\nworld\n"), form("Alice", "Bob"))
	if code != 400 || out["ok"] != false {
		t.Errorf("not an export: %d %v", code, out)
	}
	code, out = e.upload(t, "c.txt", []byte(importChat), map[string]string{"chat": "Alice", "me": "Bob", "date_order": "xyz"})
	if code != 400 {
		t.Errorf("bad date order: %d %v", code, out)
	}
	code, out = e.upload(t, "c.txt", []byte(importChat), map[string]string{"chat": "Alice", "me": "Bob", "names": "{not json"})
	if code != 400 {
		t.Errorf("bad names: %d %v", code, out)
	}
	code, _ = e.upload(t, "", nil, form("Alice", "Bob"))
	if code != 400 {
		t.Errorf("no file: %d", code)
	}
	if n := e.count(t, importAlice); n != 0 {
		t.Errorf("%d rows stored by failed imports", n)
	}
}

func TestImportRefusesUnsafeAndOversizedUploads(t *testing.T) {
	e := newImportEnv(t)
	fields := map[string]string{"chat": "15550100002", "me": "Bob", "timezone": "UTC"}

	evil := zipBytes(t, map[string][]byte{"WhatsApp Chat with Alice.txt": []byte(importChat), "../evil.txt": []byte("x")})
	if code, out := e.upload(t, "x.zip", evil, fields); code != 400 || !strings.Contains(out["error"].(string), "leaves the folder") {
		t.Errorf("traversal: %d %v", code, out)
	}
	bomb := zipBytes(t, map[string][]byte{"WhatsApp Chat with Alice.txt": []byte(importChat), "big.bin": make([]byte, 24<<20)})
	if code, out := e.upload(t, "x.zip", bomb, fields); code != 400 || !strings.Contains(out["error"].(string), "zip bomb") {
		t.Errorf("bomb: %d %v", code, out)
	}
	e.cfg.Import.MaxMB = 1
	big := bytes.Repeat([]byte("13/01/2024, 09:00 - Alice: filler filler filler filler\n"), 100000)
	if code, out := e.upload(t, "big.txt", big, fields); code != 413 {
		t.Errorf("oversized upload: %d %v", code, out)
	}
	if n := e.count(t, importAlice); n != 0 {
		t.Errorf("%d rows stored", n)
	}
	if left, _ := filepath.Glob(filepath.Join(e.cfg.StoreDir, "import-tmp", "*")); len(left) != 0 {
		t.Errorf("temporary upload left behind: %v", left)
	}
}

func TestImportNeedsTokenAndOneAtATime(t *testing.T) {
	e := newImportEnv(t)
	req, _ := http.NewRequest("POST", e.srv.URL+"/api/v1/import", strings.NewReader("{}"))
	resp, err := e.srv.Client().Do(req)
	if err != nil || resp.StatusCode != 401 {
		t.Fatalf("no token: %v %v", resp, err)
	}
	resp.Body.Close()
	req, _ = http.NewRequest("GET", e.srv.URL+"/api/v1/import", nil)
	if code, _ := e.send(t, req); code != 405 {
		t.Errorf("GET: %d", code)
	}
	importMu.Lock()
	code, out := e.postJSON(t, map[string]any{"path": "x"})
	importMu.Unlock()
	if code != 409 {
		t.Errorf("parallel import: %d %v", code, out)
	}
}

func TestOpenAPIDescribesImport(t *testing.T) {
	spec := string(openAPISpec)
	for _, want := range []string{"/import:", "operationId: importChat", "multipart/form-data", "ImportReport:"} {
		if !strings.Contains(spec, want) {
			t.Errorf("openapi.yaml lacks %q", want)
		}
	}
}
