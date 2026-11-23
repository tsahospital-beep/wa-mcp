# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

- Add an offline demo with eight fictional chats, searchable voice transcripts and a daily task briefing.
  Start it with `uvx wa-mcp-server --demo` or set `WA_MCP_DEMO=1`.
- Expose 11 core MCP tools by default. Select additional toolsets with `mcp.toolsets`,
  `WA_MCP_TOOLSETS` or `--toolsets`. Use `all` to keep the full catalog after upgrading.
- Add reactions, edits to own text, deletion of own messages for everyone and text forwarding
  to the bridge HTTP API, Python SDK and opt-in MCP `actions` toolset. Actions follow the sending policy.

## [0.1.1] - 2026-11-23

Clearer tool descriptions and annotations, glama.json.

## [0.1.0] - 2026-11-23

Initial release.

### Added

- Go bridge `wa-bridge` that links one WhatsApp account as a linked device and keeps a local SQLite store.
  A store belongs to one account, a different account is refused, and only one bridge can use a store at a
  time.
- Store folder limited to the current user (protected access list on Windows, mode 0700 and 0600 on Unix),
  with a warning when `store_dir` is outside the home folder.
- Login window on 127.0.0.1 with a QR code and pairing by phone number. Its link carries a one-time token
  that is exchanged for an HttpOnly session cookie. The terminal prints the address once and the same QR
  code as a fallback. `--login-preview` shows the window with fake codes.
- Storage of live messages, the history the phone sends after linking, receipts and calls. Edits and
  deletions are applied, deleted messages are hidden unless `include_deleted` is set, and reactions are kept
  and shown read-only. Locations, contacts and polls are stored as typed text summaries.
- On-demand history requests to the phone for older messages (`request_chat_history`, `sync_chat_history`,
  `sync_all_history`). The two sync tools and `transcribe_chat_audio` stop at a time budget (default 120
  seconds, at most 600) and return resume information.
- Attachment download for images, video, audio, voice notes, documents and stickers, with a re-upload
  request to the phone when the WhatsApp copy has expired, a size limit (`media.max_download_mb`) and
  streaming to a temporary file.
- Local HTTP API v1 on the loopback interface with bearer token and an OpenAPI 3.1 document
  (`docs/reference/openapi.yaml`).
- Sending pipeline for text, files, voice notes and stickers, with replies and mentions. Sending is off by
  default. The MCP send tools ask the user through MCP elicitation when the client supports it and can accept
  `confirm=true` when `sending.allow_confirm_flag` is enabled. Recipients must be a JID, a phone number, an alias or an exact address-book name. Files
  must be in `sending.allowed_media_dirs` (by default `downloads_dir` and `exports_dir`), and the store
  folder and the config file are never sent.
- Human-like sending: mark read first, typing indicator based on message length, pauses between messages,
  quiet hours with queue or refuse, per-chat, hourly and daily limits that are reserved before a send, and
  duplicate protection with keyed hashes.
- Read receipts on request (`read_receipts.allow_mark_read` can switch it off), optional automatic marking,
  and optional mark-read before a reply.
- Scheduled messages: once, daily, weekly or monthly, sent by the bridge without a running assistant. An
  entry keeps its kind (media, voice note or sticker) and its pacing flag, and interrupted entries are
  handled safely at the next start.
- Webhooks for `message`, `receipt`, `call`, `connection`, `scheduled` and `security` events.
  With a secret set, the bridge signs `<timestamp>.<delivery id>.<raw body>` with HMAC-SHA256. Every URL has its own
  queue, retries follow 1, 5 and 25 seconds, and redirects are not followed.
- Call history with direction, video flag, outcome and duration.
- Warnings for unknown keys in `config.json`, and a `config.example.json` that lists every key with its
  default.
- `wa-bridge import-legacy` to copy chats and messages from an older bridge database.
- Python SDK `wa_mcp`: bridge client, read access to the local database, transcription, document text
  extraction and Markdown exports.
- Voice transcription backends: faster-whisper, openai-whisper, whisper.cpp, OpenAI-compatible HTTP
  endpoints and custom commands. Install scripts for Windows and Unix.
- Text extraction from PDF, DOCX, XLSX, PPTX, plain text and HTML files, listing of ZIP and RAR archives, and
  text of public Google Docs links.
- Chat analytics, task detection, a task tracker with a `stale` status, mention search and the `my_day`
  overview.
- MCP server `wa-mcp` (stdio) with 43 tools. Tools carry read-only and destructive hints, names from the
  address book are shown plainly and push names with a leading `~`, and tools that return other people's text
  add an untrusted-data note.
- Documentation, configuration reference, generated tool reference and GitHub issue and pull request
  templates.
- Embedding: the Go package `bridge/pkg/wabridge` runs the bridge inside another program (`Main` for the
  command line, `Run` without one). `OnMessageStored` and `OnChatStored` report message and chat updates.
  Message hooks carry the origin (live, history, sent, edit or delete) and the context of the write.
  `Routes` adds HTTP handlers. A handler without a token is reachable only from loopback. `Deps` gives those
  routes the sending pipeline (with `AsDocument`), media download to a chosen path, history requests,
  profile pictures and read access to the database. `RoutesHandler` tests routes without WhatsApp. New key
  `session_file` (default `session.db`) names the session database inside `store_dir`, so a store of an
  older bridge keeps its linked device. On the Python side `wa_mcp_server.embed.register` adds chosen tools,
  optionally renamed, to another FastMCP server. `wa-bridge` itself is now a thin wrapper around
  `wabridge.Main`. Guide: [Embed the bridge](docs/guides/embedding.md).
- Several WhatsApp numbers in one MCP server: an optional `accounts` block in `config.json` adds accounts
  next to `default`, which keeps using the top-level settings, so a config without `accounts` works as
  before. Each account has its own bridge process, store folder, API address, login port and `sending`
  settings and can override the shared webhook list. Sending limits count per number. `wa-bridge --account NAME` runs one
  account, `wa-bridge accounts` lists them with their status, and `wa-bridge run-all` runs one bridge per
  account, starts a bridge again with a growing pause when it stops, passes Ctrl+C and SIGTERM on and stops
  only its own children by their process IDs. The login window shows the account in its subtitle and can
  open the login window of another account through a one-time link from that account's bridge; its session
  cookie name carries the port, so two login windows in one browser keep their sessions. Every MCP
  tool takes an optional `account` argument, the new `list_accounts` tool shows linked and connected state,
  the masked number and the store folder, `my_day` without `account` merges all accounts, and the Python SDK
  has `config.load(path, account=...)`, `Config.for_account`, `db.use_account` and `wa_mcp.accounts`. Guide:
  [Several accounts](docs/guides/accounts.md).
- Remote mode: `wa-mcp serve --http` serves MCP over the Streamable HTTP transport at `/mcp`, next to stdio.
  Authentication is required: static per-client tokens (`wa-mcp token create|list|revoke`, shown once,
  stored as HMAC-SHA256 with a server pepper in `store/server.db`) and, with `server.oauth.enabled`, OAuth 2.1
  for claude.ai custom connectors and other clients: protected resource and authorization server metadata
  (RFC 9728, RFC 8414), limited and rate-limited dynamic client registration (RFC 7591), authorization code
  with PKCE S256 only and exact redirect URI match, a consent page approved by an admin password or a
  one-time approval link from the log or `wa-mcp oauth pending`, short-lived access tokens, refresh token
  rotation with reuse detection, and revocation (RFC 7009). Rate limits per token and per address, a
  request size limit, Host and Origin allow-lists and no CORS unless `server.cors_origins` is set. Plain
  HTTP on a non-loopback address is refused unless `server.public_url` is https (a TLS proxy) or
  `--insecure-lan` is given; behind a proxy on a non-loopback address `server.trusted_proxies` is required
  and only those peers and loopback may connect. Client registration accepts only the redirect addresses in
  `server.oauth.redirect_uri_prefixes` (default: the documented Claude callbacks and loopback), at most
  `server.rate_limit.registrations_per_ip_per_hour` per address; `server.oauth.max_clients` counts approved
  clients, unapproved ones are kept for an hour (at most 10). Errors of never-approved clients are not
  redirected. The consent page shows when and from which address a client registered, and wrong admin
  passwords slow down their address. `wa-mcp health` checks `/health` of the server and of the bridge. The
  HTTP client never logs request lines (they name chats). New config section `server` and the guides
  [Connect remotely](docs/guides/remote-clients.md) and [Remote mode security](docs/security/remote.md).
- Server deployment: `deploy/docker` has Dockerfiles for the bridge (static build, distroless, non-root) and
  the server (Python slim with uv, non-root), a Compose file with read-only containers, health checks,
  restart with the bridge, `TZ`, log rotation, the database key as a Docker secret (`secrets/make-key.sh`)
  and an optional Caddy profile for automatic HTTPS, and a preview override without WhatsApp. Not yet built
  on a real Docker host: CI builds both images and starts the preview stack on pushes to `main`. Guide:
  [Run on a server](docs/guides/server.md). Dependabot watches Go modules, uv, GitHub Actions and the
  Docker images.
- Bridge: `GET /health` without a token, also during `--login-preview`,
  and `wa-bridge health` for container health checks. `login.public.url` and `login.public.listen` (env
  `WA_MCP_LOGIN_PUBLIC_URL`) serve the login page behind a TLS proxy under a path prefix, reachable only with
  the one-time link from `wa-bridge login-url`, with Secure cookies and no local window.
- Import of old chats: `wa-bridge import-export` and `POST /api/v1/import` (form upload or a path inside
  `downloads_dir`, `exports_dir` or `import.allowed_dirs`) read the files of WhatsApp's "Export chat" (`.txt`
  or `.zip` with media) and store them in one chat, for history older than the phone sync. Android and iPhone
  layouts are read with day-month-year, month-day-year and year-month-day dates (separators `/`, `.`, `-`),
  12 and 24 hour clocks, multi-line messages, direction marks and byte order marks. The date order is
  detected from the file or set with `date_order`, and a time zone can be given. System lines and media
  placeholders are recognized (English plus a few other languages, extendable with
  `import.omitted_phrases`). Messages get stable ids, so the same file can be imported again, and messages
  that the phone already synced are skipped. Zips are checked for unsafe names, links, size and
  compression ratio before use. The `import_chat_export` tool and `Bridge.import_export` do the same
  from an assistant and from Python. New config section `import` (`allowed_dirs`, `max_mb`,
  `omitted_phrases`) and the guide [Import an exported chat](docs/guides/import.md).
- Text from photos and screenshots: the `read_images` tool reads image text with a local OCR engine
  (`vision.ocr`: `tesseract`, `rapidocr` from the new optional extra `ocr`, or `none`). The text is stored
  with the message, shown by `list_messages` as `image_text`, found by search and returned by
  `get_media_text`. Images without text are remembered, and a call stops at a count limit or a time budget.
- Optional AI provider (`ai.provider`: `ollama`, `openai-compatible` or `anthropic`, default `none`) for
  background work. Remote providers require HTTPS. Keys come from environment variables. `my_day` takes `summary=true` for a
  short briefing, and `read_images` takes `describe=true` for image descriptions. `ai.max_calls_per_day` limits daily calls
  using `store/insight.db`. `ai.max_input_chars` limits prompt size and `ai.timeout_s` sets the timeout.
  The summary prompt marks chat text as untrusted. The vision prompt treats image text as untrusted.
- `bridge_status` reports `ocr_engine`, `ocr_ready`, `ocr_hint` and `ai_provider`.
- New config sections `ai` and `vision`, and the guides [AI features and providers](docs/guides/ai-providers.md)
  and [Text from photos and screenshots](docs/guides/images.md).

### Security

- Field encryption of `wa.db` is now on by default. `encryption.mode` picks the key: `keystore` (default) a
  random 32-byte store key kept for the current user by Windows DPAPI (`store/key.dpapi`), the macOS
  Keychain (`security`) or the Linux Secret Service (`secret-tool`); `passphrase` (Argon2id, as before);
  `keyfile` (`encryption.key_file` or `WA_MCP_KEY_FILE`, mode 0600 or 0400 on Unix, for containers); `off`.
  For keystore and keyfile modes, HKDF-SHA256 derives the field key from the store key.
  The database records its key kind. Reading a copy
  of encrypted fields requires the original key or passphrase. Without a usable keystore a new store uses the
  passphrase or the key file when set, and the bridge otherwise refuses to start instead of storing plain
  text. The Python package reads the same keys (DPAPI through ctypes, the same command line tools), with
  shared test vectors in `testdata/keystore_vectors.json`.
- When encryption is enabled, the bridge encrypts plain fields in `wa.db` in place. It makes a plain
  backup, verifies the encrypted fields in one transaction, wipes the backup and compacts the database.
  Existing passphrase stores remain readable. `encryption.enabled=true` selects passphrase mode when
  no mode is set. To disable encryption, set `encryption.mode` to `off`.
- The API token is kept in the OS keystore in mode `keystore` (`store/api.token.dpapi` on Windows); an
  existing `store/api.token` is moved there and wiped. `wa-bridge api-token` prints the token for scripts.
- Windows EFS for the store folder, including `session.db`, with `encryption.efs` (default `true`); the
  state is logged and shown in the status. The decision not to keep the session in memory only is explained
  in [Security and privacy](docs/security.md#why-the-session-is-not-kept-in-memory-only).
- Session events: a remote logout, another client with this session's keys, devices added to or removed
  from the account (compared after each connect and every 6 hours), a different account at login and a new
  link are logged, kept (the last 20) in `wa.db`, shown in `GET /api/v1/status` (`security_events`,
  `security_alert`, `protection`) and `bridge_status`, and sent as the new webhook event `security`.
- New env overrides `WA_MCP_ENCRYPTION_MODE` and `WA_MCP_KEY_FILE`. The key file is never sent as media.
- Threat model with test coverage in [docs/security/audit.md](docs/security/audit.md), and the guide
  [Encryption and keys](docs/guides/encryption.md).
- The root `.gitignore` excluded `bridge/internal/store/` through the pattern `store/`, so the store
  package was missing from the repository; it is tracked again.
- Field values are bound to their place: new values use the prefix `wae2:` with AES-GCM associated data
  `table|column|conv|msg_id` (plus `|source` for `derived_text`), so a value copied to another row or
  column does not open. `wae1:` values stay readable. Shared vectors in `testdata/crypt_vectors_v2.json`.
- Schema 5: `wa_contacts` and `wa_lid_map` hold a copy of the names (sealed) and the LID map from
  `session.db`, with `own_jid` and `own_lid` in meta, refreshed at start, on name events and every 10
  minutes. The MCP server reads these tables in `wa.db`. Schema 6 adds multipart send progress.
  `meta.min_reader_schema` remains 5. The SDK refuses a store that requires a newer reader.
- The encryption migration takes an exclusive lock when it can, wipes the plain copy right after the
  checked commit and also when a crash left it, retries busy checkpoints and records an unfinished
  compaction (`protection.compaction`, retried at the next start). The WAL files of `wa.db` and
  `session.db` are capped at 64 MiB.
- With one account, `session_file` accepts an absolute path for a volume that only the bridge mounts.
  The bridge restricts newly created folders and the session files. The send deny list now covers the store folders of
  all accounts and the key file.
- The API token no longer falls back to a new plain file when the keystore does not answer in mode
  `keystore`; the bridge retries and then stops, unless `api.token_file_fallback` is set.
- Key files and `WA_MCP_PASSPHRASE_FILE` (the passphrase variable name plus `_FILE`) are checked for size
  and for access by other accounts, also on Windows (user and SYSTEM only).
- Extra routes of embedders: patterns that could match bridge paths through wildcards are refused, and
  routes without a token answer loopback callers only.
- Webhook envelopes carry `account` and the masked own number `self`; `accounts.<name>.webhooks` sets a
  list per account.
- Login links expire after 10 minutes, at most 5 are open, and login sessions end after 12 hours.
- Reused attachments are checked against their SHA-256 (imports: the zip CRC-32); partial downloads and
  import uploads are wiped on failure and swept at start.
- Reaction pruning uses an index and runs the count cap at most once a minute.
- The device check also counts own devices that WhatsApp addresses by LID.
- Exit code 78 for configuration and key errors; `run-all` does not restart such a bridge, sends SIGTERM to
  its children when it dies on Linux and keeps them in a kill-on-close job object on Windows.
- The Python side reads names, the LID map and the own addresses from `wa.db` (`wa_contacts`, `wa_lid_map`,
  `meta.own_jid`/`own_lid`, copied by the bridge), so the MCP server no longer opens `session.db`. Stores
  from an older bridge are read from `session.db` with a deprecation warning. Sealed values bound to their
  place (`wae2:`, associated data `table|column|conv|msg_id`, plus the source for `derived_text`) are read
  and written; `wae1:` values still open. A store whose `meta.min_reader_schema` is newer than the package
  (now 5) is refused with a message to update.
- The reader checks the encryption state on every connection, so a bridge that encrypts the store while
  the server runs is followed at once. The passphrase can come from `<passphrase variable>_FILE`.
- Messages from imported chat exports carry `imported: true` in tool results, and task detection,
  the tracker and `my_day` leave them out unless `tasks.include_imported` is true.
- The image tool checks the header and limits images to 50 megapixels. OCR accepts JPEG, PNG, GIF, WebP
  and BMP. AI descriptions accept JPEG, PNG, GIF and WebP. The AI provider needs https for a host other than loopback, and answers larger
  than 2 MiB are refused. The untrusted-content note also names transcripts, image text, image descriptions
  and AI summaries.

[0.2.0]: https://github.com/tsahospital-beep/wa-mcp/compare/v0.1.1...main
[0.1.1]: https://github.com/tsahospital-beep/wa-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tsahospital-beep/wa-mcp/releases/tag/v0.1.0
