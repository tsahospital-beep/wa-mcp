# Security and privacy

WA MCP runs on your computer and keeps your WhatsApp data there. This page lists what is stored, what
listens on the network, what is protected and what is not. To report a problem, read
[SECURITY.md](../SECURITY.md).

## Summary

- The API and the login window listen on 127.0.0.1 only.
- The API needs a token. The login window needs a one-time link that is exchanged for a session cookie.
- The store folder is limited to your user, with a protected access list on Windows and mode 0700 on macOS
  and Linux.
- Sending is off by default. When it is on, you approve each send in your client if it supports that. The
  `confirm=true` fallback is set by the model itself and is a weaker check.
- There is no telemetry. The software makes no network requests of its own except the ones listed below.
- Field encryption of the message database is on by default. Its key and the API token are kept in the
  OS keystore of your user (Windows DPAPI, macOS Keychain, Linux Secret Service), so a copy of the store
  folder on another computer or in another Windows account cannot be read. It does not cover everything.
  Read the limits.
- On Windows the store folder, including the WhatsApp session, is also encrypted with EFS by default. On
  macOS and Linux, full disk encryption protects the session file.
- The bridge records session events (a remote logout, another client with this session, a new device on
  the account) and shows them in `bridge_status`, the status endpoint and a `security` webhook.
- Anyone who can run code as your user, including an administrator or malware, can read your data and your
  token. Check **Linked devices** on your phone regularly.

A threat model with the tests that cover each point is in [Security audit](security/audit.md).

## What is stored where

| Path | Contents | Protected by field encryption |
| --- | --- | --- |
| `store/session.db` | The keys and identity of the linked device. Whoever has this file can act as that device. | No. Windows EFS and the access list protect it; see [The WhatsApp session](#the-whatsapp-session). |
| `store/wa.db` | Chats, messages, calls, scheduled messages, reactions, a log of sent messages for limits, transcripts and extracted file text, and a copy of the contact names and the LID map from the session (`wa_contacts`, `wa_lid_map`), so the MCP server never opens `session.db`. | The text fields listed below, and the names, by default. |
| `store/insight.db` | Task tracker, analytics cache and history sync progress, written by the Python side. | With encryption on, no message text is kept in it. Names, ids and timestamps are stored in plain form. |
| `store/key.dpapi` | Windows: the store key, wrapped with DPAPI for your user. | Only your Windows user on this computer can unwrap it. |
| `store/api.token.dpapi` | Windows: the API token, wrapped the same way. | Same as above. |
| `store/keystore.id` | macOS and Linux: the random id that names this store's keychain or Secret Service items. | Not secret. |
| `store/api.token` | The API token, 64 hex characters, only in modes other than `keystore` or without a usable keystore. | No |
| `store/bridge.lock` | Lock file that keeps a second bridge away from the store. | Not applicable |
| `downloads/` | Attachments you saved, as ordinary files, in one folder per chat. | No |
| `exports/` | Markdown transcripts and analytics reports you asked for, and the temporary folders for converted voice notes and stickers. | No |

Things that stay readable even with encryption on: the chat JIDs (which contain phone numbers), chat titles
and group names, sender addresses, timestamps, message kinds and sizes, the call log, the paths of saved
files (`local_path`) and the names of the folders in `downloads/`, which are the chat JIDs. With encryption
on, documents are saved as `document_<message id>.<ext>`, so a file name does not appear in its path.

Messages that were deleted for everyone stay in `wa.db`. Reads hide them unless `include_deleted` is set,
so the deleted text is still on your disk. An edit replaces the stored text.

Chat exports that you import (see [Import an exported chat](guides/import.md)) become ordinary rows of
`wa.db`, with ids that start with `imp-`, and the field encryption applies to them. The export file
itself stays where you put it, as plain text, and so do the media in the zip, so delete or move the file
once it is imported. The import reads a file by path only from `downloads_dir`, `exports_dir` and
`import.allowed_dirs`, never from the store folder, and it refuses zips with names that leave the
folder, links, an unpacked size above `import.max_mb` or a compression ratio above 100.

### Permissions

At every start the bridge limits the whole `store` folder to your user: on Windows to a protected access
list for your account and SYSTEM, and on macOS and Linux to mode 0700 for folders and 0600 for files. The
`downloads` folder and the files the bridge writes there get the same treatment. The bridge warns when
`store_dir` is outside your home folder, because the parent folder may be readable by others. Exports,
analytics reports, `insight.db` and converted outgoing media are written by the Python side with mode 0700
for folders and 0600 for files (on Windows the folder's access list decides). Exports and reports are
plain text even when the database is encrypted, so keep `exports_dir` in a private folder.

A store belongs to one WhatsApp account. The bridge records the account at the first link and refuses a
different one later. Only one bridge can use a store at a time.

### The WhatsApp session

`session.db` holds the identity keys and the Signal session state of the linked device. It is written by
the WhatsApp library on every message, so the bridge cannot encrypt single fields in it. It is protected
like this:

- The access list (Windows) or mode 0600 (macOS, Linux) limits it to your user, at every start and again
  right after the WhatsApp library creates it. `session_file` can point to an absolute path, for example a
  volume that only the bridge container mounts; that folder is limited to your user in the same way, and
  the file and its `-wal` and `-shm` files are never sent.
- Only the bridge reads it. The MCP server gets contact names, push names and the LID map from the copy
  in `wa.db`, which the bridge refreshes at start, on name events and every 10 minutes.
- Its write-ahead log is capped at 64 MiB after each checkpoint, like the one of `wa.db`.
- On Windows, `encryption.efs` (on by default) turns on EFS for the store folder: the folder is marked so
  new files are encrypted, and existing files are encrypted at start, as `cipher /e` does. EFS keys belong
  to your Windows account, so a copied file cannot be read elsewhere. Where EFS is not available (Windows
  Home, FAT drives, network shares, a policy), the bridge logs that and carries on. The state is shown as
  `protection.efs` in the status.
- On macOS and Linux there is no comparable per-file encryption for a normal user. Turn on FileVault
  (macOS) or full disk encryption such as LUKS (Linux). Without it, a stolen disk or an unencrypted backup
  of the store folder gives the session away.

#### Why the session is not kept in memory only

A mode that keeps `session.db` in memory and writes an encrypted snapshot from time to time was
considered and not built. The session file changes with every message sent and received: the Signal
ratchet keys, pre-keys and sender keys move forward, and the server and other devices move with them. A
crash, a power cut or a killed process loses every change since the last snapshot. The bridge would then
come back with old ratchet state: messages fail to decrypt, contacts see "waiting for this message",
pre-keys may be used twice and WhatsApp may unlink the device. Writing the snapshot on every change would
just be an encrypted database with extra failure modes. EFS on Windows and full disk encryption elsewhere
protect the file at rest without that risk.

## Network

### What listens

| Component | Address | Protection |
| --- | --- | --- |
| HTTP API | `api.listen`, default `127.0.0.1:8080` | Bearer token, compared in constant time. The Host header must match the listen address, which blocks DNS rebinding. Request bodies are limited to 256 KiB. The bridge warns at start when the address is not a loopback address, and says more when it accepts connections on every interface. |
| Login window | `127.0.0.1`, random port | The address carries a one-time token. The first visit exchanges it for an HttpOnly, SameSite=Strict cookie and the token is spent. A link works for 10 minutes, at most 5 links are valid at once (a new one ends the oldest), and a session ends after 12 hours. Every later request needs the cookie, and every POST also needs a separate header token. The Host header is checked, the content security policy is strict, and the page uses a temporary browser profile that is deleted when the window closes. It only runs while no device is linked. |
| MCP server | none | It talks to the client over standard input and output. With `wa-mcp serve --http` it listens on `server.listen` (default `127.0.0.1:8765`) and needs a bearer token; see [Remote mode security](security/remote.md). |

The terminal shows the login address once and the bridge log never contains it. The token is spent as soon
as the window the bridge opened loads it, so a copy of the address is useless afterwards.

Do not set `api.listen` to an address that other machines can reach. The API has no TLS, and the token
gives full control, including sending when it is enabled.

Programs that embed the bridge (`pkg/wabridge`) can add routes. A route pattern that could match `/`,
`/health` or anything under `/api/v1`, also through a wildcard such as `/{p...}`, is refused when it is
added. Routes added without a token answer only callers on a loopback address; other callers need the
token. The Host header cannot tell the two apart, because a remote client chooses it.

### What connects out

- The bridge connects to WhatsApp, as any WhatsApp client does. Downloads of attachments and profile
  pictures come from WhatsApp servers.
- `fetch_shared_docs` downloads public Google Docs, Sheets and Slides that appear in chats, only when you
  or the assistant call it. The request is made from your computer, so Google sees your IP address.
- The `http` transcription backend sends the audio of your voice notes to `transcription.http.url`. The
  default is a server on your own machine. If you point it to a remote service, that service receives the
  audio.
- Webhooks send events, including message text, to the URLs you configure.
- When `ai.provider` is set, `my_day` with `summary=true` sends names, item types, due hints and short
  clipped context lines to that provider, and `read_images` with `describe=true` sends the image. The
  default provider address is your own machine for Ollama. A hosted provider receives the data. Both
  features are off unless you call them. See [AI features and providers](guides/ai-providers.md).
- The Whisper libraries download a model on first use, unless you downloaded it ahead of time.
- `go build` and `uv sync` download dependencies when you install.

Nothing is sent to the authors of this project.

## Webhooks

Each request carries `X-WA-MCP-Timestamp`, `X-WA-MCP-Delivery` and `X-WA-MCP-Signature`. The signature is
an HMAC-SHA256 over `<timestamp>.<delivery id>.<body>` with your secret, so a receiver that checks the
signature, rejects timestamps older than 5 minutes and drops delivery ids it has seen refuses recorded
requests that are sent again. Without a secret the requests are not signed,
and the bridge logs a warning. The bridge does not follow redirects, and every URL has its own queue, so a
failing endpoint cannot delay another. See [Webhooks](guides/webhooks.md).

## Sending safeguards

- `sending.enabled` is `false` until you change it. With it off, the send endpoints return 403.
- With `sending.require_confirm` on, the MCP send tools ask you through the client (MCP elicitation) when
  the client supports it. The question names the recipient and shows the content. When the client cannot
  ask, the tool needs `confirm=true`. The model sets that flag, so a model that is being steered by text in
  a message can set it without asking you. If your client has no approval question, treat sending as
  something that the assistant can do on its own, and keep it off unless you need it.
- Recipients for sending must be a JID, a phone number, an alias from `people.aliases` or the exact name of
  an address-book contact. Push names and chat titles are never used, because other people choose them. A
  name that matches several contacts sends nothing. Results show `to` and `to_name`.
- Files to send must be inside `downloads_dir`, `exports_dir` (always allowed) or a folder listed in
  `sending.allowed_media_dirs`. Files inside `store_dir` and the config file are never sent, whatever the
  list says, also not through hard links or (on Windows) junctions. Paths are compared after resolving
  symbolic links.
- The bridge enforces limits per chat, per hour and per day, duplicate protection and quiet hours, no
  matter which client sends. Concurrent sends reserve their place in the limits first. See
  [Sending](guides/sending.md).
- Sending paces itself like a person. This makes automated sending look less mechanical. It does not
  guarantee anything about how WhatsApp treats an account.
- Tools carry hints for clients: read-only tools are marked read-only, and tools that send, schedule,
  cancel or mark messages read are marked as changing data.
- `read_receipts.allow_mark_read` set to `false` blocks read receipts on request.

## Untrusted content

Messages, transcripts, file text, reactions, contact names, chat titles and linked documents are written
by other people. They can contain instructions aimed at your assistant. The server tells clients to treat
all of it as data, and tools that return such text add a note saying so. A name that starts with `~` is a
push name that the person chose for themselves. This reduces the risk of prompt injection and does not
remove it. To limit the damage:

- Leave sending off unless you need it.
- Use a client that asks you to approve each send, and read the question.
- Use `sending.allowed_media_dirs` and keep the limits low.
- Prefer a client that shows each tool call before it runs.

The analytics tools mask secret-looking text (passwords, codes, tokens, card and account numbers) in the
examples they return.

## Files and links

- Received files are read, not executed. File names from senders are sanitized: separators, control and
  bidirectional characters and reserved Windows device names are replaced, and each saved file carries its
  message id in the name.
- Attachments are streamed to a temporary file and renamed when complete. Files larger than
  `media.max_download_mb` (512 by default) are refused.
- Text extraction reads Office files as ZIP and XML, PDF with `pypdf`, and plain text and HTML. Archives
  (ZIP and RAR) are listed and never unpacked to disk. Executable file types are refused. Limits apply to
  file size (50 MiB), extracted text, the number of archive members, the total uncompressed size and the
  compression ratio.
- Link fetching accepts `https://docs.google.com` documents only. It refuses credentials in the URL,
  non-default ports and any address that is not public, re-checks every redirect, connects to the address
  it checked so that a changed DNS answer cannot redirect it, and limits the size and the time of the
  response.
- The `command` transcription backend runs the program you configured, without a shell.

## Field encryption

Field encryption is on by default (`encryption.mode` `keystore`). The bridge encrypts these fields in
`wa.db` with AES-256-GCM:

- message text,
- attachment names,
- quoted text of replies,
- reactions,
- the text of edits and deletions that arrive before their message,
- the text of scheduled messages,
- media keys,
- transcripts and extracted file text.

Where the key comes from depends on the mode:

| Mode | Key |
| --- | --- |
| `keystore` (default) | A random 32-byte store key, made at the first start and kept in the OS keystore of your user: Windows DPAPI in user scope (`store/key.dpapi`), the macOS login Keychain, or the Linux Secret Service through `secret-tool`. The field key is HKDF-SHA256 of the store key and a random salt stored in the database. |
| `passphrase` | Argon2id (3 passes, 64 MiB, 2 threads) of the passphrase in the variable named by `encryption.passphrase_env`, with a random salt stored in the database. |
| `keyfile` | Like `keystore`, with the store key read from `encryption.key_file`, which must have mode 0600 or 0400 on macOS and Linux. For containers. |
| `off` | No encryption. An encrypted database is refused. |

The database records which kind of key it uses (`meta.crypt_kind`), and an encrypted database always
opens with that kind. Each value gets its own random nonce. New values (prefix `wae2:`) are also bound to
their place: the table, the column and the row (chat and message id, and for derived text its source) are
the associated data of the encryption, so a value copied into another row or column does not open. Values
written before (`wae1:`) stay readable. A check value lets the bridge reject a wrong key at start. A store copied to another computer or Windows user stops with a message, because the
keystore there has no key for it. In mode `keystore` on a system without a usable keystore, a new store
uses the passphrase or the key file when one is set, and otherwise the bridge refuses to start; it never
falls back to plain text by itself. The Python side finds the key the same way. Setup, containers and
recovery are in [Encryption and keys](guides/encryption.md).

A plain database with messages, for example from an older version, is encrypted in place at the first
start. A plain copy is written first. The bridge then takes an exclusive lock on the file when no other
program has it open, encrypts all fields in one transaction and opens each value again to check it. The
copy is then overwritten and deleted, and the file is rewritten so that no plain text stays in free pages
or the write-ahead log. If anything fails before the commit, nothing is changed and the copy is kept. A
copy left by a crash is deleted at the next start. When another program (such as the MCP server) holds the
file open in a long read, the rewrite can stay unfinished; the bridge then logs a warning, shows
`protection.compaction` as `pending` and finishes it at the next start. Stop the MCP server before the
first start of a new version to avoid that.

Key files and passphrase files (`encryption.key_file`, and `WA_MCP_PASSPHRASE_FILE` or the passphrase
variable name plus `_FILE`, for Docker secrets) must be at most 4 KiB and readable by your user only:
mode 0600 or 0400 on macOS and Linux, and on Windows an access list with your user and SYSTEM only. Any
other entry, such as `BUILTIN\Administrators`, is refused with an `icacls` command that fixes it.

In mode `keystore` the API token is kept in the keystore too. When the keystore does not answer, the bridge
asks again a few times and then stops with an error instead of making a new plain token that the clients
would not know. `api.token_file_fallback` allows the plain file instead.

The log of sent messages that is used for duplicate protection stores keyed hashes, not text. With
encryption on the key is derived from the store key, and without it a random key is kept in the database,
so the hashes do not reveal which texts were sent.

The Python side keeps no message text in `insight.db` while encryption is on. When encryption is turned on
for a store that already has one, the first start wipes the copied task text and the analytics cache. The
analytics cache then stays in memory only.

Limits you should know about:

- It is field encryption, not full database encryption. Chat ids, titles, timestamps, message kinds,
  sizes, saved file paths, the call log and the structure of the database stay readable.
- `session.db` is not field-encrypted (see [The WhatsApp session](#the-whatsapp-session)). Downloads,
  exports and log output are not encrypted.
- The OS keystore protects the key from other users and other computers, not from programs running as
  you: any process of your user can ask DPAPI, the Keychain (through `security`) or the Secret Service for
  it. A passphrase in an environment variable is visible to your user's processes and to whoever reads the
  file where you set it. The key is held in memory while the programs run.
- A database that has been encrypted is refused in mode `off` and with a wrong key or passphrase.
- There is no recovery. If the key is lost (a Windows password reset by an administrator, a deleted
  profile or keychain item, a lost passphrase or key file), the encrypted fields are gone.

For protection of the whole folder, use full disk encryption of the operating system.

## Session events

The bridge watches for signs that someone else uses, or tries to use, the WhatsApp session of the store,
and records them:

| Event | Level | When |
| --- | --- | --- |
| `session_replaced` | warning | Another client connected with the keys of this session. Unless you run a second bridge with a copy of the store, the session was copied. |
| `logged_out` | warning | WhatsApp unlinked this device, for example from **Linked devices** on the phone. |
| `device_added` | warning | A new device appeared in the account's own device list. The bridge compares the list after each connect and every 6 hours, from what the WhatsApp library knows from device notifications. |
| `device_removed` | info | A device left the account's device list. |
| `account_mismatch` | warning | A different account tried to link to, or start with, this store. |
| `paired` | info | This bridge was linked as a new device. |

Each event is logged as a warning or a note, kept in `wa.db` (the last 20, also after a restart), returned
in `security_events` by `GET /api/v1/status` and by `bridge_status`, sets `security_alert` for 7 days when
it is a warning, and is sent to webhooks as event `security`. Details mask phone numbers and never contain
message text or keys. The WhatsApp library emits no event when a device is added to your own account; it
only updates its device cache, which is why the bridge compares the list. `IdentityChange` is about other
people's primary devices, so it is not used.

## Restarts and leftovers

- A bridge that stops because of its configuration, a wrong or missing key, a database of a newer version
  or a store of another account exits with code 78. `wa-bridge run-all` does not start it again, because it
  would fail the same way; other exits are restarted with a growing pause.
- On Linux the bridges started by `run-all` get SIGTERM when `run-all` dies, and on Windows they are in a
  job object that ends them when `run-all` ends, so a killed supervisor leaves no bridge running.
- Partial downloads (`.part-*`) and import uploads hold plain media and chat text. They are overwritten and
  deleted when a download or import fails, and leftovers from a crash are deleted at the next start.
- A saved attachment is used again only when its size and, when WhatsApp sent it, its SHA-256 match; a
  file from an imported export only when its size and CRC-32 match the zip entry. Otherwise it is fetched
  or unpacked again.
- No account can send files from the store folder of any other account, or the key file.

## What is not protected

- An administrator, or malware running as your user, can read the store folder, ask the OS keystore for
  the key and the token, read the passphrase variable, and use the API while the bridge runs. The access
  list on the store folder keeps other accounts out. It does not keep out your own account.
- Backups and cloud sync of the folder copy everything in it, including `session.db`. Field encryption
  keeps the message text in the copy unreadable elsewhere; EFS files stay encrypted in Windows backups. On
  macOS and Linux, keep the folder out of synced locations or encrypt the backup.
- Anyone with `session.db` can use your WhatsApp session until you remove the device on your phone.
- Message content that you give to a cloud AI assistant leaves your machine according to that assistant's
  terms. WA MCP cannot control this.
- Deleted messages stay in the database, and exports contain whatever you asked them to contain.

## Recommendations

1. Turn on full disk encryption and lock your computer.
2. Keep `api.listen` on a loopback address. Do not share the API token.
3. Keep sending off. If you need it, use a client that asks you to approve each send, and set
   `allowed_media_dirs`, quiet hours and low limits.
4. Open **Linked devices** in WhatsApp now and then. The bridge appears as `WA MCP`. Remove entries you do
   not recognize.
5. If you suspect that `session.db` or the token leaked, or `bridge_status` shows a `session_replaced` or
   `device_added` warning you cannot explain, remove the device in WhatsApp, delete the `store` folder and
   link again.
6. Keep the repository folder out of cloud sync and shared drives, and keep `exports_dir` private.
