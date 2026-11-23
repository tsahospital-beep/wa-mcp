package wabridge

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

const exportSample = `13/01/2024, 09:00 - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.
13/01/2024, 09:00 - Alice: Good morning
13/01/2024, 09:01 - Bob: Morning
13/01/2024, 09:05 - Alice: IMG-20240113-WA0001.jpg (file attached)
Lunch menu
14/01/2024, 18:30 - Alice: See you tomorrow
`

func importSetup(t *testing.T) (dir, cfgPath string) {
	t.Helper()
	dir = t.TempDir()
	cfgPath = filepath.Join(dir, "config.json")
	if err := os.WriteFile(cfgPath, []byte(`{"store_dir": "store"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	return dir, cfgPath
}

func writeZip(t *testing.T, path string) {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, data := range map[string]string{"_chat.txt": exportSample, "IMG-20240113-WA0001.jpg": "jpeg bytes"} {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		w.Write([]byte(data))
	}
	zw.Close()
	if err := os.WriteFile(path, buf.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestImportExportCommand(t *testing.T) {
	dir, cfgPath := importSetup(t)
	file := filepath.Join(dir, "WhatsApp Chat with Alice.zip")
	writeZip(t, file)
	args := []string{"import-export", "--file", file, "--chat", "15550100002", "--me", "Bob", "--timezone", "UTC", "--config", cfgPath}

	var out, errOut bytes.Buffer
	if code := run(append(args, "--dry-run"), &out, &errOut); code != 0 {
		t.Fatalf("dry run: exit %d, %q", code, errOut.String())
	}
	if !strings.Contains(out.String(), "dry run: nothing was written") || !strings.Contains(out.String(), "messages would be added: 4") {
		t.Fatalf("dry run output %q", out.String())
	}
	if _, err := os.Stat(filepath.Join(dir, "downloads")); !os.IsNotExist(err) {
		t.Errorf("a dry run wrote files: %v", err)
	}

	out.Reset()
	if code := run(args, &out, &errOut); code != 0 {
		t.Fatalf("import: exit %d, %q", code, errOut.String())
	}
	for _, want := range []string{"messages added: 4", "attachments: 1 added", "system lines left out: 1", "date order dmy (detected)"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output lacks %q:\n%s", want, out.String())
		}
	}
	cfg, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	db, err := store.Open(cfg.MessagesDB())
	if err != nil {
		t.Fatal(err)
	}
	list, _ := db.ListMessages(context.Background(), store.MessageQuery{Conv: "15550100002@s.whatsapp.net", Ascending: true})
	db.Close()
	if len(list) != 4 || list[2].Kind != store.KindImage || !strings.HasPrefix(list[2].LocalPath, cfg.DownloadsDir) {
		t.Fatalf("stored rows %+v", list)
	}

	out.Reset()
	if code := run(append(args, "--json"), &out, &errOut); code != 0 {
		t.Fatalf("second import: exit %d, %q", code, errOut.String())
	}
	var rep map[string]any
	if err := json.Unmarshal(out.Bytes(), &rep); err != nil || rep["messages_added"] != 0.0 || rep["duplicates"] != 4.0 {
		t.Fatalf("second import report %v %v", rep, err)
	}
}

func TestImportExportCommandErrors(t *testing.T) {
	dir, cfgPath := importSetup(t)
	file := filepath.Join(dir, "chat.txt")
	os.WriteFile(file, []byte(exportSample), 0o600)
	var out, errOut bytes.Buffer

	if code := run([]string{"import-export", "--config", cfgPath}, &out, &errOut); code != 2 {
		t.Errorf("missing flags: exit %d", code)
	}
	if !strings.Contains(errOut.String(), "--dry-run") || !strings.Contains(errOut.String(), "import-export --file FILE --chat CHAT") {
		t.Errorf("usage text: %q", errOut.String())
	}
	errOut.Reset()
	if code := run([]string{"--help"}, &out, &errOut); code != 0 || !strings.Contains(errOut.String(), "wa-bridge import-export") {
		t.Errorf("main help does not list the command: exit %d", code)
	}
	errOut.Reset()
	if code := run([]string{"import-export", "--file", filepath.Join(dir, "none.txt"), "--chat", "15550100002", "--config", cfgPath}, &out, &errOut); code != 1 ||
		!strings.Contains(errOut.String(), "file not found") {
		t.Errorf("missing file: exit %d %q", code, errOut.String())
	}
	errOut.Reset()
	if code := run([]string{"import-export", "--file", file, "--chat", "15550100002", "--timezone", "Nowhere/Land", "--config", cfgPath}, &out, &errOut); code != 2 {
		t.Errorf("bad zone: exit %d %q", code, errOut.String())
	}
	errOut.Reset()
	if code := run([]string{"import-export", "--file", file, "--chat", "Nobody", "--config", cfgPath}, &out, &errOut); code != 1 ||
		!strings.Contains(errOut.String(), "no stored chat is titled") {
		t.Errorf("unknown chat: exit %d %q", code, errOut.String())
	}
	errOut.Reset()
	if code := run([]string{"import-export", "--file", file, "--chat", "15550100002", "--config", cfgPath}, &out, &errOut); code != 1 ||
		!strings.Contains(errOut.String(), "cannot tell which sender is you") {
		t.Errorf("unknown me: exit %d %q", code, errOut.String())
	}
	errOut.Reset()
	// A store file is never imported.
	cfg, _ := config.Load(cfgPath)
	os.MkdirAll(cfg.StoreDir, 0o755)
	secret := filepath.Join(cfg.StoreDir, "notes.txt")
	os.WriteFile(secret, []byte(exportSample), 0o600)
	if code := run([]string{"import-export", "--file", secret, "--chat", "15550100002", "--me", "Bob", "--config", cfgPath}, &out, &errOut); code != 1 ||
		!strings.Contains(errOut.String(), "never imported") {
		t.Errorf("store file: exit %d %q", code, errOut.String())
	}
}

func TestImportExportRefusedWhileBridgeHoldsStore(t *testing.T) {
	dir, cfgPath := importSetup(t)
	file := filepath.Join(dir, "chat.txt")
	os.WriteFile(file, []byte(exportSample), 0o600)
	cfg, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	release, err := lockStore(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	var out, errOut bytes.Buffer
	code := run([]string{"import-export", "--file", file, "--chat", "15550100002", "--me", "Bob", "--config", cfgPath}, &out, &errOut)
	if code != 1 || !strings.Contains(errOut.String(), "another bridge is using") || !strings.Contains(errOut.String(), "import_chat_export") {
		t.Fatalf("exit %d, %q", code, errOut.String())
	}
}
