package keystore

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// The test binary doubles as a fake security / secret-tool program: the tests
// put links named after them on PATH, and TestMain dispatches on the name.
func TestMain(m *testing.M) {
	switch strings.TrimSuffix(filepath.Base(os.Args[0]), ".exe") {
	case "security":
		os.Exit(fakeSecurity(os.Args[1:]))
	case "secret-tool":
		os.Exit(fakeSecretTool(os.Args[1:]))
	}
	os.Exit(m.Run())
}

func fakeDB() (map[string]string, func()) {
	path := os.Getenv("FAKE_KEYSTORE_DB")
	items := map[string]string{}
	if b, err := os.ReadFile(path); err == nil {
		json.Unmarshal(b, &items)
	}
	f, _ := os.OpenFile(path+".argv", os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	fmt.Fprintln(f, strings.Join(os.Args, " "))
	f.Close()
	return items, func() {
		b, _ := json.Marshal(items)
		os.WriteFile(path, b, 0o600)
	}
}

func flagValues(args []string) map[string]string {
	out := map[string]string{}
	for i := 0; i < len(args); i++ {
		if strings.HasPrefix(args[i], "-") && i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
			out[args[i]] = args[i+1]
			i++
		} else {
			out[args[i]] = ""
		}
	}
	return out
}

func fakeSecurity(args []string) int {
	items, save := fakeDB()
	defer save()
	if os.Getenv("FAKE_KEYSTORE_FAIL") != "" {
		fmt.Fprintln(os.Stderr, "security: SecKeychainCopyDefault: User interaction is not allowed.")
		return 36
	}
	if len(args) == 0 {
		return 2
	}
	switch args[0] {
	case "-i":
		sc := bufio.NewScanner(os.Stdin)
		for sc.Scan() {
			f := strings.Fields(sc.Text())
			if len(f) == 0 || f[0] != "add-generic-password" {
				return 2
			}
			v := flagValues(f[1:])
			items[v["-s"]+"/"+v["-a"]] = v["-w"]
		}
		return 0
	case "find-generic-password", "delete-generic-password":
		v := flagValues(args[1:])
		key := v["-s"] + "/" + v["-a"]
		val, ok := items[key]
		if !ok {
			fmt.Fprintln(os.Stderr, "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.")
			return 44
		}
		if args[0] == "delete-generic-password" {
			delete(items, key)
			return 0
		}
		if _, print := v["-w"]; print {
			fmt.Println(val)
		} else {
			fmt.Println("keychain: \"login.keychain-db\"")
		}
		return 0
	}
	return 2
}

func fakeSecretTool(args []string) int {
	items, save := fakeDB()
	defer save()
	if os.Getenv("FAKE_KEYSTORE_FAIL") != "" {
		fmt.Fprintln(os.Stderr, "secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY")
		return 1
	}
	if len(args) == 0 {
		return 2
	}
	attrs := args[1:]
	if args[0] == "store" && len(attrs) > 0 && strings.HasPrefix(attrs[0], "--label=") {
		attrs = attrs[1:]
	}
	if len(attrs) != 4 || attrs[0] != "service" || attrs[2] != "account" {
		return 2
	}
	key := attrs[1] + "/" + attrs[3]
	switch args[0] {
	case "lookup":
		val, ok := items[key]
		if !ok {
			return 1
		}
		fmt.Print(val)
	case "store":
		b, _ := io.ReadAll(os.Stdin)
		items[key] = string(b)
	case "clear":
		delete(items, key)
	default:
		return 2
	}
	return 0
}

// fakePath puts fake security and secret-tool programs first on PATH and
// returns the file that receives their argument lists.
func fakePath(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "bin")
	if err := os.MkdirAll(bin, 0o700); err != nil {
		t.Fatal(err)
	}
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ext := ""
	if runtime.GOOS == "windows" {
		ext = ".exe"
	}
	for _, name := range []string{"security", "secret-tool"} {
		dst := filepath.Join(bin, name+ext)
		if err := os.Link(exe, dst); err != nil {
			data, err := os.ReadFile(exe)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(dst, data, 0o700); err != nil {
				t.Fatal(err)
			}
		}
	}
	db := filepath.Join(t.TempDir(), "fake-keystore.json")
	t.Setenv("FAKE_KEYSTORE_DB", db)
	t.Setenv("FAKE_KEYSTORE_FAIL", "")
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	return db + ".argv"
}

func TestCLIBackendsWithFakePrograms(t *testing.T) {
	argv := fakePath(t)
	for _, mk := range []func(string) Backend{Keychain, SecretService} {
		dir := t.TempDir()
		b := mk(dir)
		if err := Available(b); err != nil {
			t.Fatalf("%s: available: %v", b.Name(), err)
		}
		if _, err := StoreKey(b, false); !errors.Is(err, ErrNotFound) {
			t.Fatalf("%s: missing key: %v", b.Name(), err)
		}
		key, err := StoreKey(b, true)
		if err != nil || len(key) != 32 {
			t.Fatalf("%s: create: %v", b.Name(), err)
		}
		again, err := StoreKey(b, false)
		if err != nil || !bytes.Equal(again, key) {
			t.Fatalf("%s: read back: %v", b.Name(), err)
		}
		if _, err := os.Stat(filepath.Join(dir, IDFile)); err != nil {
			t.Fatalf("%s: no store id file: %v", b.Name(), err)
		}
		token := []byte(strings.Repeat("ab", 32))
		if err := b.Put(ItemAPIToken, token); err != nil {
			t.Fatal(err)
		}
		if got, err := b.Get(ItemAPIToken); err != nil || !bytes.Equal(got, token) {
			t.Fatalf("%s: token: %q %v", b.Name(), got, err)
		}
		if err := b.Delete(ItemAPIToken); err != nil {
			t.Fatal(err)
		}
		if _, err := b.Get(ItemAPIToken); !errors.Is(err, ErrNotFound) {
			t.Fatalf("%s: deleted token: %v", b.Name(), err)
		}
		if err := b.Delete(ItemAPIToken); err != nil {
			t.Fatalf("%s: deleting a missing item: %v", b.Name(), err)
		}
		// A copy of the folder in another place keeps its id, so the same items are found.
		moved := filepath.Join(t.TempDir(), "moved")
		os.MkdirAll(moved, 0o700)
		id, _ := os.ReadFile(filepath.Join(dir, IDFile))
		os.WriteFile(filepath.Join(moved, IDFile), id, 0o600)
		if got, err := StoreKey(mk(moved), false); err != nil || !bytes.Equal(got, key) {
			t.Fatalf("%s: moved folder: %v", b.Name(), err)
		}
		log, _ := os.ReadFile(argv)
		if bytes.Contains(log, []byte(hex.EncodeToString(key))) || bytes.Contains(log, token) {
			t.Fatalf("%s: a secret appeared in an argument list", b.Name())
		}
	}
}

func TestCLIBackendUnavailable(t *testing.T) {
	fakePath(t)
	t.Setenv("FAKE_KEYSTORE_FAIL", "1")
	for _, b := range []Backend{Keychain(t.TempDir()), SecretService(t.TempDir())} {
		if err := Available(b); !errors.Is(err, ErrUnavailable) {
			t.Fatalf("%s: %v", b.Name(), err)
		}
		if _, err := StoreKey(b, true); err == nil {
			t.Fatalf("%s: created a key without a keystore", b.Name())
		}
	}
	t.Setenv("PATH", t.TempDir())
	if err := Available(SecretService(t.TempDir())); !errors.Is(err, ErrUnavailable) || !strings.Contains(err.Error(), "not installed") {
		t.Fatalf("missing program: %v", err)
	}
}

func TestParseAndReadKeyFile(t *testing.T) {
	key := bytes.Repeat([]byte{7}, 32)
	for name, data := range map[string][]byte{
		"hex":    []byte(hex.EncodeToString(key) + "\n"),
		"base64": []byte(base64.StdEncoding.EncodeToString(key)),
		"raw":    key,
	} {
		got, err := ParseKey(data)
		if err != nil || !bytes.Equal(got, key) {
			t.Fatalf("%s: %v", name, err)
		}
	}
	for _, bad := range []string{"", "short", strings.Repeat("zz", 32), strings.Repeat("a", 65)} {
		if _, err := ParseKey([]byte(bad)); err == nil {
			t.Fatalf("accepted %q", bad)
		}
	}
	path := filepath.Join(t.TempDir(), "store.key")
	if err := fsutil.WriteFile(path, []byte(hex.EncodeToString(key))); err != nil {
		t.Fatal(err)
	}
	if got, err := ReadKeyFile(path); err != nil || !bytes.Equal(got, key) {
		t.Fatalf("read: %v", err)
	}
	if _, err := ReadKeyFile(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Fatal("missing file accepted")
	}
	// A file with the folder's default access (on Windows: also Administrators) is refused.
	open := filepath.Join(t.TempDir(), "open.key")
	os.WriteFile(open, []byte(hex.EncodeToString(key)), 0o644)
	_, err := ReadKeyFile(open)
	if err == nil || !strings.Contains(err.Error(), map[bool]string{true: "icacls", false: "chmod 600"}[runtime.GOOS == "windows"]) {
		t.Fatalf("a file others can read: %v", err)
	}
	big := filepath.Join(t.TempDir(), "big.key")
	fsutil.WriteFile(big, make([]byte, 5000))
	if _, err := ReadKeyFile(big); err == nil || !strings.Contains(err.Error(), "larger") {
		t.Fatalf("large file: %v", err)
	}
	pw := filepath.Join(t.TempDir(), "pass")
	fsutil.WriteFile(pw, []byte("correct horse\n"))
	if got, err := ReadPassphraseFile(pw); err != nil || got != "correct horse" {
		t.Fatalf("passphrase file: %q %v", got, err)
	}
	for body, want := range map[string]string{
		"pw\r\n": "pw", "pw\r": "pw\r", " pw \n\n": " pw \n",
	} {
		fsutil.WriteFile(pw, []byte(body))
		if got, err := ReadPassphraseFile(pw); err != nil || got != want {
			t.Fatalf("%q: %q %v", body, got, err)
		}
	}
}
