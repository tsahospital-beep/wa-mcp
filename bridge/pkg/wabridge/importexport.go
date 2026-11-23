package wabridge

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/importer"
)

// nameFlags collects repeated --name "Sender=JID or phone" values.
type nameFlags map[string]string

func (n nameFlags) String() string { return fmt.Sprint(map[string]string(n)) }

func (n nameFlags) Set(v string) error {
	name, to, ok := strings.Cut(v, "=")
	if !ok || strings.TrimSpace(name) == "" || strings.TrimSpace(to) == "" {
		return errors.New(`want "Sender name=JID or phone number"`)
	}
	n[strings.TrimSpace(name)] = strings.TrimSpace(to)
	return nil
}

func importExport(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("import-export", flag.ContinueOnError)
	fs.SetOutput(stderr)
	file := fs.String("file", "", "the exported chat: a .txt file or a .zip with the .txt and the media")
	chat := fs.String("chat", "", "chat to import into: JID, phone number with country code, or exact title of a stored chat")
	me := fs.String("me", "", "the sender name that is you, as the export shows it")
	order := fs.String("date-order", "", "dmy, mdy or ymd (default: detected from the file)")
	zone := fs.String("timezone", "", "time zone the export was made in, such as Europe/Berlin (default: local time)")
	dry := fs.Bool("dry-run", false, "read the file and report what would happen; write nothing")
	asJSON := fs.Bool("json", false, "print the report as JSON")
	configPath := fs.String("config", "", "path of config.json")
	account := fs.String("account", "", `account from "accounts" in the config (default "default")`)
	names := nameFlags{}
	fs.Var(names, "name", `map a sender name to a person: "Sender name=JID or phone number" (repeatable)`)
	fs.Usage = func() {
		fmt.Fprint(stderr, "Usage: wa-bridge import-export --file FILE --chat CHAT [--me NAME] [--date-order dmy|mdy|ymd]\n"+
			"                              [--timezone ZONE] [--name SENDER=JID]... [--dry-run] [--json]\n"+
			"                              [--config FILE] [--account NAME]\n\n"+
			"Stores the messages of a WhatsApp \"Export chat\" file in one chat, for history older than the\n"+
			"phone sync. Importing the same file again adds nothing. Stop the bridge first; while it runs,\n"+
			"use the import_chat_export tool or POST /api/v1/import instead.\n\nFlags:\n")
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if *file == "" || *chat == "" || fs.NArg() > 0 {
		fs.Usage()
		return 2
	}
	cfg, err := config.LoadAccount(config.ResolvePath(*configPath), *account)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	loc := time.Local
	if *zone != "" {
		if loc, err = time.LoadLocation(*zone); err != nil {
			fmt.Fprintf(stderr, "error: --timezone %q is not a zone name (use a name such as Europe/Berlin or UTC)\n", *zone)
			return 2
		}
	}
	in, err := importer.OpenInput(*file, nil, cfg.Sending.DeniedPaths)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	defer in.Close()
	st, err := in.Stat()
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}

	release, err := lockStore(cfg)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n(while the bridge runs, use the import_chat_export tool or POST /api/v1/import)\n", err)
		return 1
	}
	defer release()
	ctx := context.Background()
	db, err := openStore(ctx, cfg, nil)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	defer db.Close()

	rep, err := importer.Run(ctx, db, importer.Source{R: in, Size: st.Size(), Name: filepath.Base(*file)}, importer.Options{
		Chat: *chat, Me: *me, Names: names, DateOrder: strings.ToLower(strings.TrimSpace(*order)), Location: loc, DryRun: *dry,
		DownloadsDir: cfg.DownloadsDir, Private: db.Encrypted(), Omitted: cfg.Import.OmittedPhrases,
		Limits: importer.Limits{MaxBytes: cfg.MaxImportBytes(), MaxMediaBytes: cfg.MaxDownloadBytes()},
	})
	if err != nil {
		printImportError(stderr, err)
		return 1
	}
	if *asJSON {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(rep)
		return 0
	}
	printImportReport(stdout, rep)
	return 0
}

func printImportError(w io.Writer, err error) {
	fmt.Fprintf(w, "error: %v\n", err)
	var ce *importer.ChatError
	var me *importer.MeError
	switch {
	case errors.As(err, &ce):
		for _, c := range ce.Candidates {
			fmt.Fprintf(w, "  %s  %s\n", c.JID, c.Title)
		}
	case errors.As(err, &me):
		fmt.Fprintln(w, "  pass --me with one of these names")
	}
}

func printImportReport(w io.Writer, r *importer.Report) {
	if r.DryRun {
		fmt.Fprintln(w, "dry run: nothing was written")
	}
	chat := r.Chat
	if r.ChatTitle != "" {
		chat += " (" + r.ChatTitle + ")"
	}
	if r.NewChat {
		chat += ", new chat"
	}
	verb := "added"
	if r.DryRun {
		verb = "would be added"
	}
	fmt.Fprintf(w, "chat: %s\n", chat)
	fmt.Fprintf(w, "file: %s to %s, date order %s (%s), time zone %s\n", r.First, r.Last, r.DateOrder, r.DateOrderSource, r.Timezone)
	fmt.Fprintf(w, "messages %s: %d\n", verb, r.MessagesAdded)
	fmt.Fprintf(w, "duplicates skipped: %d (%d from an earlier import, %d already synced)\n", r.Duplicates, r.AlreadyImported, r.AlreadySynced)
	files := "added"
	if r.DryRun {
		files = "would be added"
	}
	fmt.Fprintf(w, "attachments: %d %s, %d missing from the archive, %d omitted by the export, %d refused\n",
		r.MediaAdded, files, r.MediaMissing, r.MediaOmitted, r.MediaSkipped)
	fmt.Fprintf(w, "system lines left out: %d\n", r.SystemLines)
	fmt.Fprintf(w, "unusable lines: %d", r.SkippedLines.Count)
	if len(r.SkippedLines.First) > 0 {
		fmt.Fprintf(w, " (first: %v)", r.SkippedLines.First)
	}
	fmt.Fprintln(w)
	for _, s := range r.Senders {
		author := s.Author
		if s.Outgoing {
			author = "you"
			if s.Author != "" {
				author += ", " + s.Author
			}
		}
		fmt.Fprintf(w, "sender %q: %d messages, stored as %s\n", s.Name, s.Messages, author)
	}
	for _, m := range r.Warnings {
		fmt.Fprintf(w, "warning: %s\n", m)
	}
}
