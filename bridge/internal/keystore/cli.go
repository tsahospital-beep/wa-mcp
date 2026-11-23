package keystore

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
)

// cliTimeout bounds one call of security or secret-tool (D-Bus activation can hang).
const cliTimeout = 15 * time.Second

// runner executes a keystore program; code is the exit status.
type runner func(program string, args []string, stdin []byte) (stdout, stderr []byte, code int, err error)

func execRun(program string, args []string, stdin []byte) ([]byte, []byte, int, error) {
	path, err := exec.LookPath(program)
	if err != nil {
		return nil, nil, -1, fmt.Errorf("%w: %s is not installed", ErrUnavailable, program)
	}
	ctx, cancel := context.WithTimeout(context.Background(), cliTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, args...)
	var out, errOut bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errOut
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	err = cmd.Run()
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return out.Bytes(), errOut.Bytes(), exit.ExitCode(), nil
	}
	if err != nil {
		return nil, nil, -1, fmt.Errorf("%w: %s: %v", ErrUnavailable, program, err)
	}
	return out.Bytes(), errOut.Bytes(), 0, nil
}

// cliBackend stores hex-encoded secrets through the security (macOS) or
// secret-tool (Linux) program. Secrets go through stdin, never the argument list.
type cliBackend struct {
	name    string
	program string
	dir     string
	run     runner
}

func newKeychain(dir string) *cliBackend {
	return &cliBackend{name: "keychain", program: "security", dir: dir, run: execRun}
}

func newSecretService(dir string) *cliBackend {
	return &cliBackend{name: "secret-service", program: "secret-tool", dir: dir, run: execRun}
}

// Keychain returns the macOS Keychain backend (used directly by tests).
func Keychain(dir string) Backend { return newKeychain(dir) }

// SecretService returns the Secret Service backend (used directly by tests).
func SecretService(dir string) Backend { return newSecretService(dir) }

func (c *cliBackend) Name() string { return c.name }

func (c *cliBackend) Describe(item string) string {
	if c.name == "keychain" {
		return "the macOS Keychain (service " + Service + ")"
	}
	return "the Secret Service (secret-tool, service " + Service + ")"
}

var idPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)

// storeID reads the id of the store folder; with create, a missing id is made.
func (c *cliBackend) storeID(create bool) (string, error) {
	path := filepath.Join(c.dir, IDFile)
	b, err := os.ReadFile(path)
	if err == nil {
		id := strings.TrimSpace(string(b))
		if !idPattern.MatchString(id) {
			return "", fmt.Errorf("%s is damaged", path)
		}
		return id, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	if !create {
		return "", ErrNotFound
	}
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	id := hex.EncodeToString(raw)
	if err := fsutil.WriteFile(path, []byte(id+"\n")); err != nil {
		return "", err
	}
	return id, nil
}

// Account is the keychain account (and secret-tool "account" attribute) of an item.
func Account(item, storeID string) string { return item + "." + storeID }

func (c *cliBackend) fail(op string, stderr []byte, code int) error {
	msg := strings.TrimSpace(string(stderr))
	if len(msg) > 200 {
		msg = msg[:200]
	}
	if msg == "" {
		msg = fmt.Sprintf("exit status %d", code)
	}
	return fmt.Errorf("%w: %s %s: %s", ErrUnavailable, c.program, op, msg)
}

// lookup runs a lookup for acct and returns the printed value.
func (c *cliBackend) lookup(acct string, print bool) ([]byte, error) {
	var args []string
	if c.name == "keychain" {
		args = []string{"find-generic-password", "-s", Service, "-a", acct}
		if print {
			args = append(args, "-w")
		}
	} else {
		args = []string{"lookup", "service", Service, "account", acct}
	}
	out, stderr, code, err := c.run(c.program, args, nil)
	switch {
	case err != nil:
		return nil, err
	case code == 0:
		return out, nil
	case c.name == "keychain" && code == 44:
		return nil, ErrNotFound
	case c.name == "secret-service" && code == 1 && len(bytes.TrimSpace(stderr)) == 0:
		return nil, ErrNotFound
	}
	return nil, c.fail("lookup", stderr, code)
}

func (c *cliBackend) Get(item string) ([]byte, error) {
	id, err := c.storeID(false)
	if errors.Is(err, ErrNotFound) {
		// No item can exist yet; a lookup of a dummy account checks that the program works.
		_, err = c.lookup("probe", false)
		if err == nil {
			err = ErrNotFound
		}
		return nil, err
	}
	if err != nil {
		return nil, err
	}
	acct := Account(item, id)
	out, err := c.lookup(acct, true)
	if err != nil {
		return nil, err
	}
	secret, err := hex.DecodeString(strings.TrimSpace(string(out)))
	if err != nil {
		return nil, fmt.Errorf("the item %s in %s is damaged", acct, c.Describe(item))
	}
	return secret, nil
}

func (c *cliBackend) Put(item string, secret []byte) error {
	id, err := c.storeID(true)
	if err != nil {
		return err
	}
	acct := Account(item, id)
	label := Service + "." + item
	value := hex.EncodeToString(secret)
	var args []string
	var stdin []byte
	if c.name == "keychain" {
		// security -i reads the command from stdin, so the value is not in the argument list.
		args = []string{"-i"}
		stdin = []byte(fmt.Sprintf("add-generic-password -U -s %s -a %s -l %s -w %s\n", Service, acct, label, value))
	} else {
		args = []string{"store", "--label=" + label, "service", Service, "account", acct}
		stdin = []byte(value)
	}
	_, _, code, err := c.run(c.program, args, stdin)
	if err != nil {
		return err
	}
	if code != 0 {
		return c.fail("store", nil, code) // stderr may echo the secret sent on stdin
	}
	return nil
}

func (c *cliBackend) Delete(item string) error {
	id, err := c.storeID(false)
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	acct := Account(item, id)
	var args []string
	if c.name == "keychain" {
		args = []string{"delete-generic-password", "-s", Service, "-a", acct}
	} else {
		args = []string{"clear", "service", Service, "account", acct}
	}
	_, stderr, code, err := c.run(c.program, args, nil)
	if err != nil {
		return err
	}
	if code != 0 && !(c.name == "keychain" && code == 44) {
		return c.fail("delete", stderr, code)
	}
	return nil
}
