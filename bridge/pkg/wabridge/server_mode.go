package wabridge

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/login"
)

// loginOptions builds the login window options; with login.public.url set the
// page is served for a TLS proxy instead of a local window.
func loginOptions(cfg *config.Config, out io.Writer) login.Options {
	return login.Options{
		Port:       cfg.Login.Port,
		OpenWindow: cfg.Login.OpenWindow && !cfg.Login.Public.Enabled(),
		Out:        out,
		PublicURL:  cfg.Login.Public.URL,
		Listen:     cfg.Login.Public.Listen,
	}
}

// servePreviewHealth answers /health on api.listen during --login-preview, so
// a container health check passes while no WhatsApp connection exists.
func servePreviewHealth(ctx context.Context, cfg *config.Config, logger *log.Logger) {
	if err := api.ServeHealth(ctx, cfg.API.Listen); err != nil {
		logger.Printf("warning: %v", err)
	}
}

// health exits 0 when the bridge on api.listen answers GET /health.
func health(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("health", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", "", "path of config.json")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	cfg, err := config.Load(config.ResolvePath(*configPath))
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	target, err := api.HealthURL(cfg.API.Listen)
	if err != nil {
		fmt.Fprintf(stderr, "error: api.listen: %v\n", err)
		return 1
	}
	resp, err := (&http.Client{Timeout: 5 * time.Second}).Get(target)
	if err != nil {
		fmt.Fprintf(stderr, "error: the bridge does not answer: %v\n", err)
		return 1
	}
	defer resp.Body.Close()
	var out struct {
		OK        bool `json:"ok"`
		Connected bool `json:"connected"`
		LoggedIn  bool `json:"logged_in"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 4<<10)).Decode(&out) != nil || resp.StatusCode != http.StatusOK || !out.OK {
		fmt.Fprintf(stderr, "error: health check answered %d\n", resp.StatusCode)
		return 1
	}
	fmt.Fprintf(stdout, "ok connected=%v logged_in=%v\n", out.Connected, out.LoggedIn)
	return 0
}
