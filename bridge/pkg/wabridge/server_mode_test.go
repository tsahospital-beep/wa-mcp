package wabridge

import (
	"bytes"
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

func TestHealthCommand(t *testing.T) {
	probe, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := probe.Addr().String()
	probe.Close()
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	os.WriteFile(cfgPath, []byte(`{"api":{"listen":"`+addr+`"}}`), 0o600)

	var out, errOut bytes.Buffer
	if code := run([]string{"health", "--config", cfgPath}, &out, &errOut); code != 1 {
		t.Fatalf("nothing listening: exit %d", code)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go api.ServeHealth(ctx, addr)
	code := 1
	for i := 0; i < 50 && code != 0; i++ {
		out.Reset()
		code = run([]string{"health", "--config", cfgPath}, &out, &errOut)
		time.Sleep(30 * time.Millisecond)
	}
	if code != 0 || !strings.Contains(out.String(), "ok connected=false") {
		t.Fatalf("health: exit %d %q %q", code, out.String(), errOut.String())
	}
}

func TestLoginOptionsForServerMode(t *testing.T) {
	cfg := config.Default()
	if o := loginOptions(cfg, nil); !o.OpenWindow || o.PublicURL != "" {
		t.Fatalf("local defaults: %+v", o)
	}
	cfg.Login.Public = config.LoginPublic{URL: "https://wa.example.test/login/", Listen: "0.0.0.0:8766"}
	if o := loginOptions(cfg, nil); o.OpenWindow || o.Listen != "0.0.0.0:8766" || o.PublicURL == "" {
		t.Fatalf("server mode: %+v", o)
	}
}
