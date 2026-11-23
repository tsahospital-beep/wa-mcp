package wabridge

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"text/tabwriter"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/accounts"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/supervise"
)

// loadAll loads every account of the config file, so that a mistake in any
// account block is reported before anything starts.
func loadAll(path string) ([]*config.Config, error) {
	first, err := config.Load(path)
	if err != nil {
		return nil, err
	}
	out := []*config.Config{first}
	for _, name := range first.AccountNames()[1:] {
		cfg, err := config.LoadAccount(path, name)
		if err != nil {
			return nil, err
		}
		out = append(out, cfg)
	}
	return out, nil
}

func accountFlags(name string, stderr io.Writer, args []string, defaultPath string) (string, int, bool) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", defaultPath, "path of config.json")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return "", 0, false
		}
		return "", 2, false
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "unknown argument %q\n", fs.Arg(0))
		return "", 2, false
	}
	return config.ResolvePath(*configPath), 0, true
}

// listAccounts prints every account with its store, API address and status.
func listAccounts(args []string, stdout, stderr io.Writer, defaultPath string) int {
	path, code, ok := accountFlags("accounts", stderr, args, defaultPath)
	if !ok {
		return code
	}
	cfgs, err := loadAll(path)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tw := tabwriter.NewWriter(stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "ACCOUNT\tSTORE\tAPI\tSTATUS")
	for _, cfg := range cfgs {
		info := accounts.Inspect(ctx, cfg)
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", info.Name, info.StoreDir, info.Listen, describe(info))
	}
	tw.Flush()
	return 0
}

func describe(info accounts.Info) string {
	var parts []string
	switch {
	case info.Running && info.Connected:
		parts = append(parts, "running, connected")
	case info.Running:
		parts = append(parts, "running, not connected")
	default:
		parts = append(parts, "not running")
	}
	if info.Linked {
		parts = append(parts, "linked")
	} else {
		parts = append(parts, "not linked")
	}
	if info.Number != "" {
		parts = append(parts, info.Number)
	}
	if info.Note != "" {
		parts = append(parts, info.Note)
	}
	return strings.Join(parts, ", ")
}

// runAll starts one bridge process per account and keeps them running until
// Ctrl+C or SIGTERM, which it passes on to each of them.
func runAll(args []string, stdout, stderr io.Writer, defaultPath string) int {
	path, code, ok := accountFlags("run-all", stderr, args, defaultPath)
	if !ok {
		return code
	}
	logger := log.New(stdout, "", log.LstdFlags)
	cfgs, err := loadAll(path)
	if err != nil {
		logger.Printf("error: %v", err)
		return 1
	}
	exe, err := os.Executable()
	if err != nil {
		logger.Printf("error: %v", err)
		return 1
	}
	children := make([]supervise.Child, 0, len(cfgs))
	for _, cfg := range cfgs {
		file, name := cfg.File, cfg.Account
		children = append(children, supervise.Child{Name: name, Cmd: func() *exec.Cmd {
			return exec.Command(exe, "--config", file, "--account", name)
		}})
		logger.Printf("run-all: account %s, store %s, api %s", name, cfg.StoreDir, cfg.API.Listen)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	sup := &supervise.Supervisor{Children: children, Out: stdout, Log: logger}
	if err := sup.Run(ctx); err != nil {
		logger.Printf("error: %v", err)
		return 1
	}
	logger.Printf("run-all: stopped")
	return 0
}
