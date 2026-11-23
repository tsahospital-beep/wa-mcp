package importer

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
	_ "time/tzdata" // zone names work on systems without a zone database (Windows)

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

// dedupeWindow is how far apart (in time) an exported message and a synced
// one can be and still count as the same message.
const dedupeWindow = 2 * time.Minute

// maxWarnings caps the warnings of one report.
const maxWarnings = 10

// Options configure Run.
type Options struct {
	// Chat is the chat to import into: a JID, a phone number or the exact title of a stored chat.
	Chat string
	// Me is the sender name that is you, as the export shows it.
	Me string
	// PushName is the display name of the linked account, used when Me is empty.
	PushName string
	// Names maps sender names to a JID or phone number.
	Names map[string]string
	// DateOrder is dmy, mdy or ymd; empty detects it from the file.
	DateOrder string
	// Location is the time zone the export was made in (local time by default).
	Location *time.Location
	// DryRun reads and counts everything and writes nothing.
	DryRun bool
	// DownloadsDir receives the attachments, in one folder per chat.
	DownloadsDir string
	// Private names documents by message id only (the store is encrypted).
	Private bool
	// Omitted adds media placeholder phrases to the built-in ones.
	Omitted []string
	Limits  Limits
}

// SenderReport says how one name of the file was stored.
type SenderReport struct {
	Name     string `json:"name"`
	Messages int    `json:"messages"`
	Author   string `json:"author"`
	Outgoing bool   `json:"outgoing"`
}

// Report is what an import did, or with DryRun what it would do.
type Report struct {
	DryRun          bool           `json:"dry_run"`
	Chat            string         `json:"chat"`
	ChatTitle       string         `json:"chat_title,omitempty"`
	NewChat         bool           `json:"new_chat"`
	MessagesAdded   int            `json:"messages_added"`
	Duplicates      int            `json:"duplicates"`
	AlreadyImported int            `json:"already_imported"`
	AlreadySynced   int            `json:"already_synced"`
	MediaAdded      int            `json:"media_added"`
	MediaMissing    int            `json:"media_missing"`
	MediaOmitted    int            `json:"media_omitted"`
	MediaSkipped    int            `json:"media_skipped"`
	SystemLines     int            `json:"system_lines"`
	SkippedLines    Skipped        `json:"skipped_lines"`
	DateOrder       string         `json:"date_order"`
	DateOrderSource string         `json:"date_order_source"`
	Timezone        string         `json:"timezone"`
	First           string         `json:"first,omitempty"`
	Last            string         `json:"last,omitempty"`
	Me              string         `json:"me,omitempty"`
	Senders         []SenderReport `json:"senders"`
	Warnings        []string       `json:"warnings,omitempty"`
}

func (r *Report) warn(format string, args ...any) {
	if len(r.Warnings) < maxWarnings {
		r.Warnings = append(r.Warnings, fmt.Sprintf(format, args...))
	}
}

// Candidate is a stored chat offered when the chat could not be resolved.
type Candidate struct {
	JID   string `json:"jid"`
	Title string `json:"title"`
}

// ChatError means the chat argument did not name exactly one chat.
type ChatError struct {
	Msg        string
	Ambiguous  bool
	Candidates []Candidate
}

func (e *ChatError) Error() string { return e.Msg }

// MeError means the sender who is you could not be told.
type MeError struct {
	Msg     string
	Senders []string
}

func (e *MeError) Error() string { return e.Msg }

// RequestError means the file or the options cannot be used: a damaged archive, a wrong format, a limit
// that is exceeded, an unknown date order or a bad entry of Names.
type RequestError struct{ Err error }

func (e *RequestError) Error() string { return e.Err.Error() }
func (e *RequestError) Unwrap() error { return e.Err }

func requestErr(err error) error {
	if err == nil {
		return nil
	}
	return &RequestError{Err: err}
}

// Run imports one export file into the chat named in the options.
func Run(ctx context.Context, db *store.DB, src Source, o Options) (*Report, error) {
	lim := o.Limits.withDefaults()
	loc := o.Location
	if loc == nil {
		loc = time.Local
	}

	var ar *archive
	var raw []byte
	var err error
	if isZip(src) {
		if ar, err = openArchive(src, lim); err != nil {
			return nil, requestErr(err)
		}
		if raw, err = readEntry(ar.chat, min(lim.MaxBytes, maxTextBytes), lim); err != nil {
			return nil, requestErr(err)
		}
	} else if raw, err = readPlain(src, min(lim.MaxBytes, maxTextBytes)); err != nil {
		return nil, requestErr(err)
	}

	po := parseOptions{Order: o.DateOrder, Loc: loc, Omitted: newOmittedSet(o.Omitted), Attached: attachedSet()}
	if ar != nil {
		po.HasFile = func(name string) bool { return ar.files[strings.ToLower(name)] != nil }
	}
	parsed, err := parseText(decodeText(raw), po)
	if err != nil {
		return nil, requestErr(err)
	}
	if len(parsed.Messages) == 0 {
		return nil, requestErr(errors.New("the file has headers but no messages (only system lines or empty entries)"))
	}

	rep := &Report{
		DryRun: o.DryRun, DateOrder: parsed.Order, DateOrderSource: parsed.OrderSource, Timezone: loc.String(),
		SystemLines: parsed.SystemLines, SkippedLines: parsed.Skipped, Senders: []SenderReport{},
	}
	if parsed.OrderSource == SourceAssumed {
		rep.warn("the date order could not be told from the file; day-month-year was assumed")
		if !o.DryRun {
			return nil, requestErr(ErrDateAmbiguous)
		}
	}

	conv, jid, err := resolveChat(ctx, db, o.Chat)
	if err != nil {
		return nil, err
	}
	rep.Chat, rep.NewChat = jid, conv == nil
	hint := titleHint(src.Name)
	if conv != nil {
		rep.ChatTitle = conv.Title
	}
	title := ""
	if conv == nil || conv.Title == "" {
		title, rep.ChatTitle = hint, hint
	}
	group := strings.HasSuffix(jid, "@g.us")

	mine, meErr := pickMe(parsed.Senders, o.Me, o.PushName, !group, []string{hint, rep.ChatTitle}, jid)
	rep.Me = mine
	if meErr != nil && (!o.DryRun || o.Me != "") {
		return nil, meErr
	}
	if meErr != nil {
		rep.warn("%s", meErr.Error())
	}
	account, err := db.AccountJID(ctx)
	if err != nil {
		return nil, err
	}
	people := &authors{ctx: ctx, db: db, chat: jid, group: group, me: fold(mine), account: account, names: map[string]string{}}
	for name, to := range o.Names {
		j, err := wa.ParseChat(to)
		if err != nil {
			return nil, requestErr(fmt.Errorf("names: %q: %w", name, err))
		}
		people.names[fold(cleanName(name))] = j.String()
	}
	for _, s := range parsed.Senders {
		a, out, err := people.resolve(s.Name)
		if err != nil {
			return nil, err
		}
		rep.Senders = append(rep.Senders, SenderReport{Name: s.Name, Messages: s.Messages, Author: a, Outgoing: out})
		if strings.HasPrefix(a, "name:") {
			rep.warn("sender %q was not matched to a person and is stored as %q", s.Name, a)
		}
	}

	w := &work{
		ctx: ctx, db: db, o: o, lim: lim, ar: ar, rep: rep, jid: jid, loc: loc, parsed: parsed,
		senders: map[string]SenderReport{},
	}
	for _, s := range rep.Senders {
		w.senders[s.Name] = s
	}
	if err := w.run(title, group); err != nil {
		return nil, err
	}
	return rep, nil
}
