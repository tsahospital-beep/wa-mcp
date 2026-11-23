# FAQ

## About the project

### Relationship to WhatsApp

WA MCP is an independent tool. It is not made, endorsed or supported by WhatsApp or Meta. It links to your
account as a companion device, the same way WhatsApp Web does, using an open-source implementation of the
multi-device protocol. Automated use of a WhatsApp account may go against the WhatsApp terms of service,
and WhatsApp can limit or block accounts. The human-like sending options make automated sending look less
mechanical. They do not guarantee anything. Use the software at your own risk.

### Where the data lives

By default, `store/`, `downloads/` and `exports/` are next to `config.json`. Change their locations with
`store_dir`, `downloads_dir` and `exports_dir`. A hosted AI or transcription provider receives the inputs
for the features that use it. The assistant you connect
sees whatever the tools return, which is covered by that assistant's terms. See [Security](security.md).

### Platforms

The CI workflow checks the Go bridge and Python server on Ubuntu, Windows and macOS. The bridge tries to
open the login window in a supported Chromium-based browser with a temporary profile. If none opens,
use `wa-bridge login-url` and open the link yourself.

## Linking and syncing

### The login window does not open

During login, run `wa-bridge login-url` for a fresh one-time link. For another account, add `--account NAME`.
An interactive terminal shows the initial link. Redirected output omits its token. You can also scan the QR
code in the terminal. `login.open_window` controls whether the bridge launches a browser.

### The QR code expires

Codes are short-lived. Click **Click to reload QR code** in the window or press Enter in the terminal.
Linking by phone number is an alternative, see [Getting started](getting-started.md).

### The bridge says it was disconnected because another client opened the session

Two programs used the same `session.db`. Run only one bridge per store folder.

### Messages from before linking are missing

After linking, the phone sends a batch of recent history. Older messages have to be requested. Use
`request_chat_history` for one page of up to 100 messages, `sync_chat_history` to page back to a date in one
chat, or `sync_all_history` for many chats. The phone must be online and answers in the background, so
results can take a few seconds per page. The two sync tools stop after `time_budget_seconds` (default 120,
at most 600). When they stop early they answer with `complete=false` and a hint to call again with the same
arguments. Progress is saved, so repeated calls continue where the last one ended.

When the phone no longer has the messages, export the chat in WhatsApp (**Export chat**) and load the file
with `wa-bridge import-export` or the `import_chat_export` tool. See [Import an exported chat](guides/import.md).

### Expired media

WhatsApp deletes media from its servers after a while. `download_media` asks your phone to upload the file
again, waits up to 40 seconds and retries. This needs the phone to be online and to still have the file.

### Phone and LID chats

WhatsApp can address one person by a phone number (`...@s.whatsapp.net`) or by an anonymous id
(`...@lid`). The tools merge both into one person, so search, analytics and the task tracker see one
conversation.

### Several accounts

One store belongs to one WhatsApp account. The bridge records the account at the first link and refuses
and unlinks a different account later. Add the second number under `accounts` in the same config and give
it its own `api.listen`. `wa-bridge run-all` starts one bridge per number. One MCP server serves them all.
See [Several accounts](guides/accounts.md).

### An older bridge database

`wa-bridge import-legacy --from path/to/messages.db` copies chats and messages from an older bridge
database into `store/wa.db`. If a `whatsapp.db` sits next to the file, it is used to match older sender
addresses, or name another file with `--session`. Existing rows are kept, and running it twice adds
nothing. Stop the bridge first, and stop any older bridge that still writes to the file, because the import
takes the store lock.

### Edits, deletions and reactions

An edit by the author replaces the stored text. A message that was deleted for everyone stays in the local
database, but reads hide it unless you pass `include_deleted`. The deleted text is therefore still on your
disk. Reactions are stored and shown read-only, as emoji counts and the names of who reacted, by
`list_messages` and `get_message_context`.

### Names with a tilde

A name that starts with `~` is a push name, which is the name a person chose for themselves. It is not from
your address book. Anyone can pick any push name, so do not trust it to identify a person. Names from your
address book and from `people.aliases` have no tilde.

## Using it

### Search in other languages

Search is case-insensitive in any script. The built-in word lists for task detection, analytics and due
dates are English. Add your words under `tasks.keywords`, `insight.words` and `insight.dates`, see
[Configuration](guides/configuration.md).

### The analytics use a language model

`chat_analytics`, `detect_tasks`, the tracker and the structured `my_day` result use local rules.
With `summary=true`, `my_day` can ask your configured AI provider to write a briefing.

### Task statuses

The tracker uses the statuses `new`, `in_progress`, `waiting`, `done`, `cancelled` and `stale`. An open task
with no activity for `tasks.stale_days` days (60 by default) becomes `stale` and is no longer shown by
`my_day`. Finished and stale tasks older than `tasks.keep_days` (365 by default) are removed from the
tracker. The tracker rebuilds from your chats, and `refresh_tasks` runs the rebuild on demand. Your
messages are not touched. `find_mentions` answers with `has_more` set to `true` when more matches exist, and
you page through them with `offset`.

### Locations, contacts and polls

These messages are stored as typed text summaries: `location` with the name and coordinates, `contact` with
the name and phone numbers, and `poll` with the question and the options. They have no attachment. They
appear in normal results with that `kind`.

### Blue ticks

Reading through an assistant does not send read receipts. Use `mark_as_read`, or turn on
`read_receipts.auto_mark_read`. A reply marks the chat read first unless you set
`sending.mark_read_before_reply` to `false`.

### Group messages

Groups appear as chats with `is_group` set. Task detection and `my_day` treat a group message as yours
only when it mentions you, replies to you or uses one of your names (`insight.my_names`).

### Sending does nothing

Check these in order: `sending.enabled` is `true` and the bridge was restarted, you approved the question
that the client showed (or, in a client without that question, `sending.allow_confirm_flag` is enabled and the call included `confirm=true`), the
recipient is a JID, a phone number, an alias or an exact address-book name, the file is in an allowed
folder, and the answer is not a limit, a duplicate or quiet hours. A queued message during quiet hours shows
up in `list_scheduled` as pending.

## Problems

| Message | What to do |
| --- | --- |
| `no database at ...: start the bridge once to create it` | Start `wa-bridge` in the same folder, or point both programs to the same config file. |
| `bridge not reachable at ...: is wa-bridge running?` | Start the bridge. Both programs must agree on `api.listen`. |
| `no API token: start the bridge once` | In the default mode the bridge stores the generated token in your OS keystore. Other modes use `store/api.token`. Start it once as the same user as the MCP server. |
| `bridge answered 401` | The token is wrong. Check `api.token` and `WA_MCP_API_TOKEN` for differences between the two programs. |
| `sending is disabled` | Set `sending.enabled` to `true`. |
| `the database is encrypted, but encryption.mode is off` | The store is encrypted. Set `encryption.mode` back to `keystore` (or the mode it was encrypted with). |
| `... holds no key for this store` | The store was copied from another computer or Windows user, or its key was removed from the keystore. It cannot be opened here; see [Encryption and keys](guides/encryption.md#moving-a-store-and-recovery). |
| `no OS keystore is available` | On Linux install `secret-tool` with a running keyring, or set `encryption.mode` to `passphrase` or `keyfile`. See [Encryption and keys](guides/encryption.md#linux-without-a-keystore). |
| `warning: ... unknown key "..." is ignored` | A key in `config.json` is not one the bridge uses, usually a typo. Compare it with the names in [Configuration](guides/configuration.md). |
| `another bridge is using ...; stop it first` | A bridge is already running on that store folder, or an import is. Stop it first. Only one process can use a store at a time. |
| `this store belongs to another WhatsApp account` | The folder is bound to the account that was linked first. Link the other account with its own `store_dir`, or start over with a fresh store folder. |
| `attachment is larger than media.max_download_mb` | Raise `media.max_download_mb` or download the file on the phone. |
| `ffmpeg not found` | Install ffmpeg or set `WA_MCP_FFMPEG`. |

## Reset and removal

To start over, open **Linked devices** in WhatsApp and remove `WA MCP`, stop the bridge and delete the
`store` folder. The next start shows the login window and binds the new store to the account you link.

To remove local data, check the configured store, download, export and session paths for each account.
They can be outside the repository. On macOS and Linux, keystore items can also live outside those paths.
See [Encryption and keys](guides/encryption.md#where-the-keys-are).
