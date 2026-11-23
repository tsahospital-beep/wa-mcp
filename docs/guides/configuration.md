# Configuration

WA MCP reads one JSON file, `config.json`. The Go bridge and the Python MCP server read the same file, so
the two always agree about folders, the API address and sending rules. [`config.example.json`](../../config.example.json)
lists every key with its default value. Copy it and delete what you do not change, or write a file with
only the keys you need.

For the optional `agent` block, see [Agent connector](agent-connector.md).

## How the file is found

The path comes from the first of these that is set:

1. `--config FILE` on `wa-bridge` or `wa-mcp`.
2. The environment variable `WA_MCP_CONFIG`.
3. `config.json` in the current folder.

A missing file means all defaults. Relative paths inside the file, such as `store_dir`, are resolved
against the folder that contains the config file, not the current folder. Start both programs with the
same config path and they share the same store.

## Rules

- The file is plain JSON. It has no comments. This page is the documentation for the keys.
- Save it as UTF-8. A byte order mark at the start is accepted.
- Missing keys keep their defaults. Nested objects are merged key by key.
- The bridge logs a warning for every key it does not use, for example `unknown key "sending.enabeld" is
  ignored`, and carries on with the default. Read the log after a change.
- The sections that only the Python side reads (`mcp`, `transcription`, `ai`, `vision`, `people`, `tasks`, `mentions`, `insight`, `server`)
  are not checked by the bridge. A wrong value in one of them is reported by the MCP server.
- A wrong value in a section the bridge uses stops the bridge at start with a message that names the
  key.

## Environment variables

| Variable | Read by | Effect |
| --- | --- | --- |
| `WA_MCP_TOOLSETS` | server | Comma-separated toolset names, for example `core,actions`. The `--toolsets` flag takes precedence. |
| `WA_MCP_CONFIG` | both | Config file when `--config` is not given. |
| `WA_MCP_STORE_DIR` | both | Overrides `store_dir`. |
| `WA_MCP_DOWNLOADS_DIR` | both | Overrides `downloads_dir`. |
| `WA_MCP_EXPORTS_DIR` | both | Overrides `exports_dir`. |
| `WA_MCP_API_LISTEN` | both | Overrides `api.listen`. |
| `WA_MCP_API_TOKEN` | both | Overrides `api.token`. |
| `WA_MCP_SENDING_ENABLED` | both | Overrides `sending.enabled`. Accepts `true`, `false`, `1`, `0`. |
| `WA_MCP_LOGIN_OPEN_WINDOW` | both | Overrides `login.open_window`. |
| `WA_MCP_ENCRYPTION_MODE` | both | Overrides `encryption.mode`. |
| `WA_MCP_KEY_FILE` | both | Overrides `encryption.key_file`, for example a Docker secret such as `/run/secrets/wa_mcp_key`. |
| `WA_MCP_PASSPHRASE` | both | Database passphrase for `encryption.mode` `passphrase`, and the fallback for `keystore` on a system without a keystore. The name is set by `encryption.passphrase_env`. |
| `WA_MCP_PASSPHRASE_FILE` | both | File with the database passphrase, read when the passphrase variable is empty (Docker secrets). The name is the passphrase variable plus `_FILE`. |
| `WA_MCP_LOG_LEVEL` | bridge | Log level of the WhatsApp library: `ERROR`, `WARN` (default), `INFO`, `DEBUG`. |
| `WA_MCP_FFMPEG` | server | Path of the ffmpeg program when it is not on `PATH`. |
| `WA_MCP_STT_API_KEY` | server | API key for the `http` transcription backend. The name is set by `transcription.http.api_key_env`. |
| `WA_MCP_AI_API_KEY` | server | API key for the AI provider. The name is set by `ai.api_key_env`. |
| `WA_MCP_LOGIN_PUBLIC_URL` | bridge | Overrides `login.public.url`. |
| `WA_MCP_SERVER_LISTEN` | server | Overrides `server.listen` (the `--listen` flag wins). |
| `WA_MCP_SERVER_PUBLIC_URL` | server | Overrides `server.public_url`. |
| `WA_MCP_ADMIN_PASSWORD` | server | Admin password for OAuth approval when `server.oauth.approval` is `password`. The name is set by `server.oauth.admin_password_env`. `WA_MCP_ADMIN_PASSWORD_FILE` names a file with it instead. |
| `WA_MCP_SERVER_PEPPER` | server | Key for the hashes in `store/server.db`, at least 32 characters. Without it the server creates `store/server.key`. `WA_MCP_SERVER_PEPPER_FILE` names a file with it instead. |

Webhook secrets are read from the variables named in each webhook's `secret_env`. The MCP server also reads
the database. In the default mode it finds the key in the OS keystore by itself, as the same user. With a
passphrase or a key file, the variable or the file must be available to the server process as well. In an
MCP client config, add the variable to the server's `env` block.

## Toolsets

Set `mcp.toolsets` to a nonempty JSON list. The default is `["core"]`, including when an older config omits
the section. Include `core` alongside any additional sets you want. Use `["all"]` for the full catalog
including all tools from 0.1.1. Each selection replaces the lower-priority selection in full.

```json
{"mcp": {"toolsets": ["core", "history", "actions"]}}
```

Both `wa-mcp --toolsets core,history,actions` and `wa-mcp serve --http --toolsets core,history,actions`
override `WA_MCP_TOOLSETS`, which overrides the file. Restart the MCP server to apply a change.
It logs the enabled sets at startup. Unknown names stop startup with an error. Tool selection controls
MCP discovery and invocation. The bridge API and explicit embedding registrations have their own entry points.
See the [tool names grouped by toolset](../reference/tools.md).

## Folders and API

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `store_dir` | string | `"store"` | Folder for `wa.db` (messages), `session.db` (WhatsApp session), `insight.db` (task tracker, analytics cache, sync progress), `api.token` and `bridge.lock`. The bridge limits it to your user. It belongs to one WhatsApp account and can be used by one running bridge at a time. Files in it are never sent. |
| `downloads_dir` | string | `"downloads"` | Saved attachments go to `downloads_dir/<chat>/`. The folder name is the chat JID with `@` replaced by `_`, for example `downloads/15550100002_s.whatsapp.net/`. Profile pictures go to `downloads_dir/avatars/`. |
| `exports_dir` | string | `"exports"` | Markdown transcripts and analytics reports. Converted voice notes and stickers are written to `exports_dir/outgoing/` for the send. |
| `session_file` | string | `"session.db"` | The WhatsApp session database: a file name inside `store_dir`, or an absolute path such as `/session/session.db` on a volume that only the bridge mounts. Set it to the file of an older bridge (for example `whatsapp.db`) to keep its linked device without a new QR code. A name with folders, `wa.db`, `insight.db`, `api.token` and `bridge.lock` are refused, and so is an absolute path inside `downloads_dir`, `exports_dir` or `sending.allowed_media_dirs`. An absolute path is supported only with one account. The bridge restricts external session files and any folder it creates for them. Use a private folder if it already exists. Schema 5 and later supply names through `wa.db`. |
| `api.listen` | string | `"127.0.0.1:8080"` | Address of the local HTTP API as `host:port`. Keep it on a loopback address. The bridge logs a warning for any other address. |
| `api.token` | string | `""` | Bearer token for the API. When empty, the bridge creates a token on first start (64 hex characters) and keeps it in the OS keystore (`encryption.mode` `keystore`), otherwise in `store/api.token`. `wa-bridge api-token` prints it. |
| `api.token_file_fallback` | boolean | `false` | In mode `keystore`: when the keystore does not answer (asked three times), keep the token in `store/api.token` instead of stopping with an error. Off by default, because a new plain token would not match the one the clients read from the keystore. |

## Login window

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `login.device_name` | string | `"WA MCP"` | Device name sent to the phone when linking. |
| `login.device_platform` | string | `"desktop"` | Built-in icon. Choose `desktop` (computer), `chrome`, `firefox`, `safari`, `edge` or `unknown`. WhatsApp does not support custom icons. Re-link to apply a change to either device setting. |
| `login.open_window` | boolean | `true` | Open the login window automatically. When false, the bridge only prints its address and the terminal QR code. |
| `login.port` | integer | `0` | Port of the login window on 127.0.0.1. `0` picks a free port. |
| `login.public.url` | string | `""` | Serve the login page behind a TLS proxy at this address, for a bridge on a server, for example `https://wa.example.com/login/`. Must be https (http only for a loopback address). Its path is the page prefix. The page then needs the one-time link from `wa-bridge login-url`, sets Secure cookies and opens no window. See [Run on a server](server.md). |
| `login.public.listen` | string | `"127.0.0.1:8766"` | Address the login page listens on when `login.public.url` is set, as `host:port`. The proxy forwards to it. |

## Accounts

More WhatsApp numbers next to the account `default`, which always exists and uses the top-level settings.
Each account runs its own bridge. See [Several accounts](accounts.md). In the keys below `<name>` is the
account name. Start with a lowercase ASCII letter or digit. Later characters may also be `-` or `_`. The limit is 32 characters. A block named `default` may change
the default account with the same keys; its folders and API address default to the top-level values.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `accounts` | object | `{}` | Maps account names to blocks with the keys below. Empty means one account, `default`, exactly as without the key. |
| `accounts.<name>.store_dir` | string | `store_dir` + `/<name>` | Store folder of this account. Every account needs its own. |
| `accounts.<name>.downloads_dir` | string | `downloads_dir` + `/<name>` | Attachments of this account. |
| `accounts.<name>.exports_dir` | string | `exports_dir` + `/<name>` | Exports of this account. |
| `accounts.<name>.api.listen` | string | required | API address of this account's bridge. Every account needs its own port. |
| `accounts.<name>.login.port` | integer | `login.port` | Port of this account's login window. Ports other than `0` must differ between accounts. |
| `accounts.<name>.sending` | object | `sending` | Keys of `sending` that differ for this number, merged key by key over the top-level `sending`. The limits count the messages of this number only. |
| `accounts.<name>.webhooks` | list | `webhooks` | Webhooks of this number. When set, the list replaces the top-level list for this account. Every event names its account in `account`. |

Every other setting, such as encryption, receipts, transcription and the insight sections, is
shared by all accounts. Environment variables change the top-level settings, so a block wins over them for
its account.

## Receipts and calls

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `read_receipts.auto_mark_read` | boolean | `false` | Mark every new incoming message as read as soon as it is stored. Off by default, so reading through an assistant leaves no blue ticks. |
| `read_receipts.allow_mark_read` | boolean | `true` | Allow the `mark_as_read` tool and the `/read` endpoint. When false, the endpoint answers 403 and nothing can send read receipts on request. |
| `calls.record` | boolean | `true` | Store call events in the call log. When false, new call events are not stored. |

## Media

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `media.max_download_mb` | integer | `512` | Largest attachment the bridge saves, in MiB. Larger ones are refused. Must be greater than 0. |

## Import

See [Import an exported chat](import.md).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `import.allowed_dirs` | list of strings | `[]` | Extra folders an export file may be read from by path (API and `import_chat_export`). `downloads_dir` and `exports_dir` are always allowed. Files inside `store_dir` and the config file are never imported. |
| `import.max_mb` | integer | `1024` | Largest export file in MiB, and the largest unpacked size of a zip. Must be greater than 0. |
| `import.omitted_phrases` | list of strings | `[]` | Extra lines that stand for a media file the export left out, such as `<Media omitted>`. They add to the built-in English and other languages. |

## Sending

Sending is off by default. See [Sending](sending.md) for how the settings work together.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `sending.enabled` | boolean | `false` | Master switch for the send tools, the send and schedule API endpoints and scheduled sends. |
| `sending.require_confirm` | boolean | `true` | Every MCP send is approved by you: through elicitation in the client when it supports it, see `allow_confirm_flag` otherwise. |
| `sending.allow_confirm_flag` | boolean | `false` | For clients without elicitation only: accept `confirm=true` from the model as approval. Off means such clients cannot send. Read by the Python package. |
| `sending.human_like` | boolean | `true` | Use the human-like waits (mark read, reading pause, typing indicator, pauses between messages). |
| `sending.mark_read_before_reply` | boolean | `true` | Before a send, mark the latest unread incoming messages of that chat as read (up to 20). |
| `sending.typing_chars_per_second` | number | `7` | Typing speed used to compute the typing time. Must be greater than 0. |
| `sending.typing_min_seconds` | number | `1.2` | Shortest typing indicator. |
| `sending.typing_max_seconds` | number | `20` | Longest typing indicator. Must be at least the minimum. |
| `sending.jitter` | number | `0.3` | Random variation of waits, from 0 up to but not including 1. `0.3` means plus or minus 30 percent. |
| `sending.pause_between_messages_seconds` | list of 2 numbers | `[1.0, 3.5]` | Minimum and maximum pause between two messages to the same chat. |
| `sending.quiet_hours.enabled` | boolean | `false` | Turn quiet hours on. |
| `sending.quiet_hours.start` | string | `"23:00"` | Start of quiet hours, `HH:MM`, local time of the machine running the bridge. |
| `sending.quiet_hours.end` | string | `"08:00"` | End of quiet hours, `HH:MM`. The window may cross midnight. |
| `sending.quiet_hours.action` | string | `"queue"` | `queue` stores the message as a scheduled message for the end of quiet hours. `refuse` rejects it. |
| `sending.limits.per_chat_per_hour` | integer | `20` | Messages to one chat in the last hour. `0` means no limit. |
| `sending.limits.per_hour` | integer | `60` | Messages to all chats in the last hour. `0` means no limit. |
| `sending.limits.per_day` | integer | `300` | Messages to all chats in the last 24 hours. `0` means no limit. |
| `sending.limits.chats_per_identical_message_per_hour` | integer | `3` | Distinct chat addresses that may receive the same text and attachment request within one hour. Reserved sends count. `0` disables this limit. |
| `sending.dedupe_window_minutes` | number | `10` | Refuse the same text or file to the same chat again within this many minutes. `0` turns it off. |
| `sending.allowed_media_dirs` | list of strings | `[]` | Extra folders that files to send may come from. `downloads_dir` and `exports_dir` are always allowed; this list adds to them. The bridge checks the resolved file against its denied paths, including all account stores, other accounts' downloads and exports, the config file, database key and passphrase files, and external session files. |

## Webhooks

`webhooks` is a list. Each entry is an object. See [Webhooks](webhooks.md).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `webhooks[].url` | string | required | `http` or `https` address that receives the events. |
| `webhooks[].events` | list of strings | all events | Any of `message`, `receipt`, `call`, `connection`, `scheduled`, `security`. An empty list means all. |
| `webhooks[].secret_env` | string | none | Name of the environment variable that holds the signing secret. Without a secret the requests are not signed. |

## Encryption

Field encryption of `wa.db` is on by default. See [Encryption and keys](encryption.md) and
[Security](../security.md) for what it covers.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `encryption.mode` | string | `"keystore"` | `keystore`: a random key kept in the OS keystore of your user (Windows DPAPI, macOS Keychain, Linux Secret Service). `passphrase`: a key from the passphrase in `passphrase_env`. `keyfile`: a key from `key_file`. `off`: no encryption. An encrypted store needs its original key kind. Mode `off` refuses it. |
| `encryption.passphrase_env` | string | `"WA_MCP_PASSPHRASE"` | Name of the environment variable with the passphrase. In mode `keystore` a set passphrase is used for a new store when the system has no keystore. |
| `encryption.key_file` | string | `""` | File with a 32-byte key as 64 hex characters, base64 or raw bytes, for mode `keyfile` (also the fallback of `keystore`). On macOS and Linux it must have mode 0600 or 0400. It is never sent. |
| `encryption.efs` | boolean | `true` | On Windows, turn on EFS for the store folder, so `session.db` and the other files are encrypted on disk for your user. Ignored elsewhere and where EFS is not available. |

The old key `encryption.enabled` still works: `true` without a `mode` means `passphrase`. `false` is
ignored with a warning; set `mode` to `off` to keep the database unencrypted.

## Transcription

See [Voice transcription](transcription.md).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `transcription.backend` | string | `"faster-whisper"` | `faster-whisper`, `openai-whisper`, `whisper.cpp`, `http` or `command`. |
| `transcription.model` | string | `"large-v3"` | Model name for `faster-whisper` and `openai-whisper`. An empty value selects `large-v3` for faster-whisper and `turbo` for openai-whisper. |
| `transcription.device` | string | `"auto"` | `auto`, `cpu` or `cuda`, as understood by the backend. |
| `transcription.compute_type` | string | `"auto"` | Compute type of faster-whisper, for example `int8` or `float16`. |
| `transcription.language` | string or null | `null` | Force a language code such as `en`. `null` or `"auto"` detects the language per clip and prefers the usual language of the chat for short or uncertain clips. |
| `transcription.chunk_seconds` | number | `30` | Long audio is cut into chunks of about this length at quiet points. |
| `transcription.whisper_cpp.binary` | string | `""` | Path of `whisper-cli`. |
| `transcription.whisper_cpp.model` | string | `""` | Path of a ggml model file. |
| `transcription.http.url` | string | `"http://127.0.0.1:8000/v1/audio/transcriptions"` | OpenAI-compatible transcription endpoint. Use HTTPS off loopback. The response limit is 4 MiB. |
| `transcription.http.model` | string | `"whisper-1"` | Model name sent to the endpoint. |
| `transcription.http.api_key_env` | string | `"WA_MCP_STT_API_KEY"` | Name of the environment variable with the API key. Its `_FILE` variant names a key file. If neither is set, no key is sent. |
| `transcription.command.argv` | list of strings | `[]` | Program and arguments. `{file}` becomes the WAV path and `{language}` the language code or `auto`. |

## AI provider

Optional. See [AI features and providers](ai-providers.md). Nothing is sent unless `ai.provider` is set and
a feature that uses it is called.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `ai.provider` | string | `"none"` | `none`, `ollama`, `openai-compatible` or `anthropic`. |
| `ai.base_url` | string | `""` | Address of the provider. Use HTTPS off loopback. The response limit is 2 MiB. Empty means `http://127.0.0.1:11434` for `ollama` and `https://api.anthropic.com` for `anthropic`. `openai-compatible` needs it, including the version part such as `/v1`. |
| `ai.text_model` | string | `""` | Model for summaries. |
| `ai.vision_model` | string | `""` | Model for image descriptions. |
| `ai.api_key_env` | string | `"WA_MCP_AI_API_KEY"` | Name of the environment variable with the API key. An unset variable sends no key. The `anthropic` provider needs one. |
| `ai.timeout_s` | number | `60` | Seconds to wait for one answer. Must be greater than 0. |
| `ai.max_calls_per_day` | integer | `200` | Calls per local day. `0` means no limit. |
| `ai.max_input_chars` | integer | `12000` | Largest input of one call, in characters. Larger input is refused. Must be at least 1. |

## Images

See [Text from photos and screenshots](images.md).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `vision.ocr` | string | `"tesseract"` | `tesseract`, `rapidocr` or `none`. |
| `vision.languages` | list of strings | `["eng"]` | Tesseract language codes, read together. Not used by `rapidocr`. |
| `vision.tesseract_binary` | string | `""` | Path of the `tesseract` program. Empty means look it up on `PATH`. |
| `vision.max_image_mb` | number | `20` | Largest image that is read or described, in MiB. Must be greater than 0. |
| `vision.timeout_s` | number | `60` | Seconds for one image with tesseract. Must be greater than 0. |
| `vision.min_text_chars` | integer | `3` | Text with fewer letters, digits or underscores counts as no text. |

## People

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `people.aliases` | object | `{}` | Maps a phone number or JID to the name tools should show, for example `{"15550100002": "Alice"}`. Aliases win over names from the address book. An alias can also be used as the recipient of a send. |

## Tasks and mentions

These keys tune the rule-based task detection, the tracker and `find_mentions`. The word lists are English.
Your words extend the lists. They do not replace them. A trailing `*` in a word means any ending, so
`crash*` matches `crash`, `crashes` and `crashed`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tasks.projects` | list of objects | `[]` | Projects used to guess which project a request belongs to. See below. |
| `tasks.keywords` | object | `{}` | Extra words per list. Lists: `request`, `urgent`, `want`, `deliverable`, `problem`, `follow_up`, `cancel`, `not_cancel`, `chat`, `spec`, `accept`, `done`, `thanks`, `progress`, `waiting`. |
| `tasks.burst_gap_minutes` | number | `4` | Messages of one sender closer than this many minutes form one request. Allowed range 0.1 to 1440. |
| `tasks.reply_window_minutes` | number | `30` | How long after a request your reply still counts as accepting it. Allowed range 1 to 1440. |
| `tasks.refresh_days` | integer | `14` | Days of chat history the tracker rebuilds from. Allowed range 1 to 3650. |
| `tasks.min_score` | integer | `60` | Minimum score (0 to 100) for a request to create a tracked task. |
| `tasks.stale_days` | integer | `60` | An open task with no activity for this many days becomes `stale`. Stale tasks are not shown by `my_day`. Allowed range 1 to 3650. |
| `tasks.keep_days` | integer | `365` | Finished (`done`, `cancelled`) and `stale` tasks older than this many days are removed from the tracker. Allowed range 1 to 36500. |
| `tasks.include_imported` | boolean | `false` | Also detect requests and promises in messages from imported chat exports (`wa-bridge import-export`). Off by default, because old exported requests are usually finished. Tools mark such messages with `imported: true`. |
| `mentions.watch` | list | `[]` | Words or phrases for `find_mentions`. Each entry is a phrase or an object `{"label": "Report", "words": ["report", "q3 numbers"]}`. |

A project entry has these keys:

| Key | Meaning |
| --- | --- |
| `id` | Required. Short identifier. |
| `name` | Display name. Defaults to the id. |
| `keywords` | Words that point to the project. Longer words weigh more. |
| `weak` | Keywords that also appear outside the project and weigh less. |
| `chats` | JIDs of chats that belong to this project only. |
| `generic` | `true` for a catch-all project that wins only when nothing specific matches. |

## Insight settings

These keys tune chat analytics and `my_day`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `insight.night_hours` | list of 2 integers | `[0, 7]` | Start hour and end hour of the night. Messages that arrive at night and wait more than five minutes for your reply are left out of the reply-speed statistics. |
| `insight.date_order` | string | `"dmy"` | `dmy` or `mdy`, for numeric dates such as `03/04` in messages. |
| `insight.turn_gap_seconds` | number | `180` | Messages of one side closer than this belong to one turn. |
| `insight.session_gap_hours` | number | `6` | A silence this long starts a new conversation session. |
| `insight.cache_days` | number | `30` | How long cached analytics results are kept. Allowed range 0.01 to 3650. |
| `insight.my_names` | list of strings | `[]` | Names other people call you. A group message that contains one counts as aimed at you. |
| `insight.words` | object | `{}` | Extra words per list. Lists: `deferral`, `promise`, `formal`, `informal`, `ask`, `greeting`, `farewell`, `laugh`, `negation`, `stopwords`. |
| `insight.dates` | object | `{}` | Extra names for due dates. Keys: `today`, `tonight`, `tomorrow`, `day_after_tomorrow`, `next_week`, `this_week`, `weekend`, `end_of_day` (lists of words), `weekdays` (7 lists, Monday first) and `months` (12 lists, January first). |

## Remote server

Read by `wa-mcp serve --http` only. See [Connect remotely](remote-clients.md) and
[Run on a server](server.md).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `server.listen` | string | `"127.0.0.1:8765"` | Address of the HTTP server as `host:port`. A non-loopback address needs an https `server.public_url` together with `server.trusted_proxies`, or `--insecure-lan`. |
| `server.public_url` | string | `""` | The https address of the TLS proxy in front of the server, such as `https://wa.example.com`, without a path. It is the OAuth issuer, and `/mcp` under it is the resource. Empty means the loopback listen address. |
| `server.allowed_hosts` | list of strings | `[]` | Extra accepted `Host` header values. The public host and `127.0.0.1`, `localhost` and `[::1]` with the listen port are always accepted. Other hosts get 421. |
| `server.allowed_origins` | list of strings | `[]` | Extra accepted `Origin` values. The public origin is always accepted, and requests without `Origin` pass. Other origins get 403. |
| `server.cors_origins` | list of strings | `[]` | Origins that get CORS headers, for browser-based clients. Empty means CORS is off. These origins are also accepted as `Origin`. |
| `server.trusted_proxies` | list of strings | `[]` | Addresses or networks (CIDR) of the TLS proxy in front of the server. For forwarded requests, rate limits use the rightmost untrusted hop in `X-Forwarded-For`. With an HTTPS public URL, a loopback listener trusts loopback by default. A non-loopback listener requires this list and restricts all routes except `/health` to listed peers and loopback unless `--insecure-lan` is set. |
| `server.max_request_kb` | integer | `256` | Largest request body in KiB, from 16 to 16384. Larger requests get 413. |
| `server.rate_limit.per_token_per_minute` | integer | `120` | Requests per bearer token. `0` means no limit. |
| `server.rate_limit.per_ip_per_minute` | integer | `240` | Failed requests per client address without a valid token. A valid token uses its own limit. `0` means no limit. |
| `server.rate_limit.auth_per_ip_per_minute` | integer | `20` | Failed `/mcp` requests, OAuth requests and consent posts without a valid bearer token, per address. `0` means no limit. |
| `server.rate_limit.registrations_per_ip_per_hour` | integer | `10` | OAuth client registrations per address and hour. `0` means no limit. |
| `server.oauth.enabled` | boolean | `false` | Serve OAuth 2.1 (discovery, registration, authorization with PKCE, token, revocation) for clients such as claude.ai custom connectors. Static tokens work either way. |
| `server.oauth.approval` | string | `"link"` | How you approve a client: `link` prints a one-time approval link to the server log (and `wa-mcp oauth pending` prints a fresh one), `password` asks for the admin password on the consent page. |
| `server.oauth.admin_password_env` | string | `"WA_MCP_ADMIN_PASSWORD"` | Name of the environment variable with the admin password. Its `_FILE` variant names a file instead. |
| `server.oauth.access_token_minutes` | integer | `60` | Lifetime of access tokens, 1 to 1440. |
| `server.oauth.refresh_token_days` | integer | `30` | Lifetime of refresh tokens, 1 to 365. Each refresh returns a new one. |
| `server.oauth.max_clients` | integer | `20` | Approved clients at most; registration is refused while that many exist. Up to 10 unapproved clients are kept separately for up to an hour. At capacity, registration replaces the oldest eligible client. If all have an open consent request, registration is refused. |
| `server.oauth.redirect_uri_prefixes` | list of strings | Claude and loopback, see the note | Clients may register only redirect URIs that start with one of these. Default: `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, `http://localhost:`, `http://127.0.0.1:`, `http://[::1]:`. `[]` allows any https, loopback http or private-use address. |

## Example

Enable sending with a few guard rails, quiet hours and a smaller transcription model. The folder list
adds one folder to the two default ones, which stay allowed:

```json
{
  "sending": {
    "enabled": true,
    "allowed_media_dirs": ["/home/you/share-out"],
    "quiet_hours": { "enabled": true, "start": "22:00", "end": "08:00", "action": "queue" },
    "limits": { "per_chat_per_hour": 10, "per_hour": 30, "per_day": 100 }
  },
  "transcription": { "model": "small" },
  "people": { "aliases": { "15550100002": "Alice" } }
}
```
