package importer

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/wa"
)

func fold(s string) string { return store.Fold(cleanName(s)) }

// resolveChat finds the chat to import into. A JID or phone number is taken as it is (the chat is
// created when it is not stored yet); anything else must be the exact title of one stored chat.
// The conversation is nil for a chat that does not exist yet.
func resolveChat(ctx context.Context, db *store.DB, chat string) (*store.Conversation, string, error) {
	chat = strings.TrimSpace(chat)
	if chat == "" {
		return nil, "", &ChatError{Msg: "chat is required: give a JID, a phone number with country code or the exact title of a stored chat"}
	}
	if j, err := wa.ParseChat(chat); err == nil {
		jid := j.String()
		c, err := db.GetConversation(ctx, jid)
		switch {
		case err == nil:
			return c, jid, nil
		case errors.Is(err, store.ErrNotFound):
			return nil, jid, nil
		}
		return nil, "", err
	}
	list, err := db.ConversationsByTitle(ctx, chat)
	if err != nil {
		return nil, "", err
	}
	if len(list) == 1 {
		return &list[0], list[0].JID, nil
	}
	if len(list) == 0 {
		near, err := db.ListConversations(ctx, chat, 10, 0)
		if err != nil {
			return nil, "", err
		}
		return nil, "", &ChatError{Msg: fmt.Sprintf("no stored chat is titled %q; give its JID or phone number", chat), Candidates: candidates(near)}
	}
	return nil, "", &ChatError{Msg: fmt.Sprintf("%d chats are titled %q; give the JID of the one you mean", len(list), chat),
		Ambiguous: true, Candidates: candidates(list)}
}

func candidates(list []store.Conversation) []Candidate {
	out := make([]Candidate, 0, len(list))
	for _, c := range list {
		out = append(out, Candidate{JID: c.JID, Title: c.Title})
	}
	return out
}

var titleFromFile = regexp.MustCompile(`(?i)^whatsapp[ _]chat(?:[ _]with|[ _]-|-)?[ _]*(.+?)(?:[ _]\(\d+\))?$`)

// titleHint reads the chat name from a file name such as "WhatsApp Chat with Alice.zip".
func titleHint(file string) string {
	base := filepath.Base(strings.ReplaceAll(file, "\\", "/"))
	base = strings.TrimSuffix(base, filepath.Ext(base))
	m := titleFromFile.FindStringSubmatch(base)
	if m == nil {
		return ""
	}
	return cleanName(strings.ReplaceAll(m[1], "_", " "))
}

// pickMe finds the sender name that is you: the one named in explicit, then the account's own push
// name. In a direct chat with two senders, the one that is not the other person (named by the file
// name, the stored title or the phone number) is you.
func pickMe(senders []Sender, explicit, push string, direct bool, hints []string, jid string) (string, *MeError) {
	names := make([]string, len(senders))
	for i, s := range senders {
		names[i] = s.Name
	}
	find := func(want string) string {
		w := fold(want)
		for _, n := range names {
			if w != "" && fold(n) == w {
				return n
			}
		}
		return ""
	}
	listed := `"` + strings.Join(names, `", "`) + `"`
	if explicit != "" {
		if n := find(explicit); n != "" {
			return n, nil
		}
		return "", &MeError{Msg: fmt.Sprintf("no sender in the file is called %q (senders: %s)", explicit, listed), Senders: names}
	}
	if push != "" {
		if n := find(push); n != "" {
			return n, nil
		}
	}
	if direct && len(names) == 2 {
		user, _, _ := strings.Cut(jid, "@")
		isOther := func(n string) bool {
			f := fold(n)
			for _, h := range hints {
				if h != "" && fold(h) == f {
					return true
				}
			}
			d := common.Digits(n)
			return len(d) >= 7 && d == user
		}
		a, b := isOther(names[0]), isOther(names[1])
		switch {
		case a && !b:
			return names[1], nil
		case b && !a:
			return names[0], nil
		}
	}
	return "", &MeError{Msg: fmt.Sprintf("cannot tell which sender is you; give the name as the export shows it (senders: %s)", listed), Senders: names}
}

// authors maps the names of the file to the author column.
type authors struct {
	ctx     context.Context
	db      *store.DB
	chat    string
	group   bool
	me      string // folded name of the sender that is you
	account string // our own JID, "" before the first login
	names   map[string]string
}

// resolve returns the author value and whether the message is outgoing.
func (a *authors) resolve(name string) (string, bool, error) {
	f := fold(name)
	if a.me != "" && f == a.me {
		return a.account, true, nil
	}
	if j, ok := a.names[f]; ok {
		return j, false, nil
	}
	if !a.group {
		// A direct chat has one other person, whatever name the file shows for them.
		return a.chat, false, nil
	}
	if j, ok := phoneJID(name); ok {
		return j, false, nil
	}
	list, err := a.db.ConversationsByTitle(a.ctx, name)
	if err != nil {
		return "", false, err
	}
	var direct []string
	for _, c := range list {
		if !c.IsGroup {
			direct = append(direct, c.JID)
		}
	}
	if len(direct) == 1 {
		return direct[0], false, nil
	}
	return "name:" + name, false, nil
}

// phoneJID turns a name that is a phone number (unsaved contacts show as numbers) into a JID.
func phoneJID(name string) (string, bool) {
	var digits strings.Builder
	for _, r := range name {
		switch {
		case r >= '0' && r <= '9':
			digits.WriteRune(r)
		case r == '+' || r == ' ' || r == '-' || r == '(' || r == ')' || r == '.' || r == 0xa0 || r == 0x202f:
		default:
			return "", false
		}
	}
	d := digits.String()
	if len(d) < 7 || len(d) > 15 || d[0] == '0' {
		return "", false
	}
	return d + "@s.whatsapp.net", true
}
