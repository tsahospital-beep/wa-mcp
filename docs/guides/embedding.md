# Embed the bridge in your own program

The `wa-bridge` command is a thin wrapper around the Go package
`github.com/tsahospital-beep/wa-mcp/bridge/pkg/wabridge`. Another Go program can run the same bridge with
the same config file and add three things:

- hooks that see every message and chat the bridge stores,
- extra HTTP routes on the bridge API,
- access to the sending pipeline, media download, history requests and the database from those routes.

Use it when you need behavior that does not belong in the bridge itself, for example a copy of the messages
in another database or an endpoint that an older program of yours still calls. On the Python side,
[`wa_mcp_server.embed.register`](#add-wa-mcp-tools-to-another-mcp-server) adds chosen MCP tools to another
FastMCP server.

## A small example

```go
package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"

	"github.com/tsahospital-beep/wa-mcp/bridge/pkg/wabridge"
)

func main() {
	opts := wabridge.Options{
		Version: "book-club-1",
		OnMessageStored: func(ctx context.Context, m wabridge.StoredMessage) {
			if m.Origin == wabridge.OriginLive && !m.Outgoing {
				log.Printf("new %s message in %s", m.Kind, m.ChatTitle)
			}
		},
		Routes: func(r wabridge.Router, d wabridge.Deps) error {
			return r.Handle("GET /api/book-club/last", http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				list, err := d.Store().Messages(req.Context(), wabridge.MessageQuery{Chat: "120363000000000001@g.us", Limit: 1})
				if err != nil || len(list) == 0 {
					http.Error(w, "nothing yet", http.StatusNotFound)
					return
				}
				fmt.Fprintln(w, list[0].Body)
			}))
		},
	}
	os.Exit(wabridge.Main(os.Args[1:], opts))
}
```

`Main` takes the command line of `wa-bridge` (`--config`, `import-legacy` and the other commands) and runs
the bridge with your options. `Run(ctx, opts)` runs the bridge without a command line until `ctx` ends.

In `go.mod`, require the module. While you work on both repositories at once, a `replace` directive points
at a local copy:

```text
require github.com/tsahospital-beep/wa-mcp/bridge v0.0.0-00010101000000-000000000000

replace github.com/tsahospital-beep/wa-mcp/bridge => ../wa-mcp/bridge
```

Only `pkg/wabridge` is meant for other modules. The packages under `bridge/internal` can change at any time.

## Options

| Field | Meaning |
| --- | --- |
| `ConfigPath` | The config file. Empty means `WA_MCP_CONFIG`, then `./config.json`. The `--config` flag of `Main` replaces it. |
| `SessionFile` | Replaces `session_file`, the name of the WhatsApp session database inside `store_dir`. |
| `ConfigSections` | Extra top-level sections of `config.json` for your program: section name to a pointer the section is decoded into. These keys are not reported as unknown. |
| `Version` | Printed by `--version` and reported by `GET /api/v1/status`. |
| `Stdout`, `Stderr` | The log and the command output. Default `os.Stdout` and `os.Stderr`. |
| `OnMessageStored` | Called after a message row was written. |
| `OnChatStored` | Called after a chat row was created, renamed or its last activity moved forward. |
| `Routes` | Called once before the API starts, to add routes. An error stops the bridge. |

## Hooks

`OnMessageStored` gets a `StoredMessage` for every write of a message row, after the row was committed. The
hooks run on the goroutine that stored the row, so keep them short; a slow hook slows down the bridge. A
hook that panics is logged and does not stop the bridge.

| `Origin` | When |
| --- | --- |
| `live` | A message arrived while the bridge was connected, including your own messages sent from the phone. |
| `history` | A message came with a history sync from the phone. |
| `sent` | The bridge sent the message (`/api/v1/send`, scheduled messages, `Deps.Send`). |
| `edit` | An edit was applied; `Body` is the new text and `EditedAt` is set. |
| `delete` | A deletion was recorded; `DeletedAt` is set, or `DeletedUnverified` when a group member who is not the author and not a known admin deleted it. `Body` still holds the text. |

The fields of `StoredMessage`:

| Field | Meaning |
| --- | --- |
| `Chat`, `ChatTitle` | Chat JID and its stored title. The title is empty for the first message of a new chat; `OnChatStored` follows. |
| `ID`, `Author`, `Outgoing`, `SentAt` | Message id, author JID (your own JID for outgoing messages), direction and time (local time zone). |
| `Body`, `Kind` | Text or caption, and one of `text`, `image`, `video`, `audio`, `voice`, `document`, `sticker`, `location`, `contact`, `poll`, `unsupported`. |
| `AttachmentName`, `Mime`, `Size` | File name of a document (`sticker_<hash>.webp` for stickers), type and size in bytes. |
| `URL`, `DirectPath`, `MediaKey`, `FileSHA256`, `FileEncSHA256` | What WhatsApp sent to download the attachment. |
| `ReplyToID`, `ReplyToAuthor`, `ReplyPreview` | The quoted message and a preview of at most 200 characters. |
| `Mentioned` | Mentioned JIDs. |
| `EditedAt`, `DeletedAt`, `DeletedUnverified` | See the table above. |

`ctx` carries the values of the context that caused the write. For a send through `Deps.Send` it is the
context you passed, so a route can mark its own sends and the hook can tell them apart:

```go
type mine struct{}

// in the route:
res, err := d.Send(context.WithValue(req.Context(), mine{}, true), wabridge.SendRequest{To: "15550100002", Text: "Hi Bob"})

// in OnMessageStored:
if ctx.Value(mine{}) != nil { /* our own send */ }
```

Hooks receive plain text also when `encryption.mode` encrypts `wa.db`. Whatever a hook writes elsewhere is
outside that protection.

## Routes

`Routes` gets a `Router` and the `Deps` of the running bridge.

- `Handle(pattern, handler)` adds a route that needs the API token, like `/api/v1`.
- `HandleWithoutToken(pattern, handler)` adds a route without the token check. The Host check (only
  `localhost`, `127.0.0.1` and `::1` with the API port, or the exact listen address) and the 256 KB body limit
  still apply. Your handler must check its callers itself, for example with its own key file.

Patterns use the `net/http` ServeMux syntax, `"/api/x"` or `"POST /api/x"`. The paths `/`, `/health` and
everything under `/api/v1` belong to the bridge; such patterns, malformed ones and duplicates return an
error. The routes share the listen address `api.listen` and the shutdown wait of the API.

## Deps

| Method | What it does |
| --- | --- |
| `Paths()` | Resolved config file, `store_dir`, `downloads_dir`, `exports_dir`, session database and `wa.db`. |
| `OwnJID()`, `Connected()` | Your phone-number JID ("" before the first login) and the connection state. |
| `Send(ctx, req)` | The sending pipeline: `sending.enabled`, limits, duplicate check, quiet hours and human-like pacing. `SendRequest` has `To`, `Text`, `MediaPath`, `AsVoice`, `AsDocument`, `StickerPath`, `ReplyToID`, `Mentions` and `Human`. `AsDocument` sends any file as a document. Files must be inside the folders that `sending.allowed_media_dirs` allows; the store folder never is. |
| `DownloadMedia(ctx, chat, id, target)` | Saves an attachment. With `target` empty it goes to `downloads_dir` as for `/api/v1/media/download`; otherwise to the file `target`. An expired copy is requested from the phone again. |
| `RequestHistory(ctx, chat, count, beforeID)` | Asks the phone for older messages, anchored on `beforeID` or on the oldest stored message. |
| `SaveProfilePicture(ctx, jid, dir)` | Saves a profile picture as `dir/avatars/<user>.jpg`. |
| `Store()` | Reads the database: `Message`, `Messages` with a `MessageQuery`, `Chat` and `Chats`. |

Errors can be compared with `errors.Is` against `ErrNotFound`, `ErrSendingDisabled`, `ErrNotConnected`,
`ErrNotLoggedIn`, `ErrBadChat`, `ErrNotMedia` and `ErrNoProfilePicture`. `IsLimit(err)` reports a send that
`sending.limits` refused.

## Test your routes

`RoutesHandler(listen, token, deps, routes)` builds an `http.Handler` with only your routes and the same Host,
token and body checks, so a test can run them against a fake `Deps` without a WhatsApp connection:

```go
h, err := wabridge.RoutesHandler("127.0.0.1:8080", "0123456789abcdef0123456789abcdef", fake, myRoutes)
```

## Session file and config sections

`session_file` in `config.json` (default `session.db`) names the WhatsApp session database inside
`store_dir`. A program that used an older bridge can point it at the old file, for example `whatsapp.db`,
so the linked device keeps working without a new QR code. The Python server reads the same key.

`ConfigSections` lets your program keep its settings in the same file:

```go
var club struct {
	Reminder string `json:"reminder"`
}
opts := wabridge.Options{ConfigSections: map[string]any{"book_club": &club}}
```

## Add wa-mcp tools to another MCP server

An MCP server written with FastMCP can take chosen wa-mcp tools:

```python
from mcp.server.fastmcp import FastMCP

from wa_mcp import config
from wa_mcp_server.embed import register

mcp = FastMCP("book-club")
cfg = config.load("config.json")
register(mcp, include=["my_day", "list_calls"], cfg=cfg, rename={"list_calls": "wa_calls"})
mcp.run("stdio")
```

`register` returns the names the tools got. `rename` gives a tool another name when your server already uses
that name. Names that no wa-mcp tool has raise `ValueError` before anything is added. `tool_names(cfg)` lists
all names. The added tools share one config per process.
