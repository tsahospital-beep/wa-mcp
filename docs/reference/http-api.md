# HTTP API

The bridge serves a local JSON API. The MCP server and the Python SDK use it for everything that needs
WhatsApp, and you can use it from any language. The machine-readable description is
[openapi.yaml](openapi.yaml), also served by the bridge at `/api/v1/openapi.yaml`.

The connector consumes live references with `POST /agent/next`, reports decisions with `POST /agent/reply` and optionally rejects an incoming direct call with `POST /agent/reject`. All three use the bridge bearer token. [Agent connector](../guides/agent-connector.md)

## Basics

- Base URL: `http://127.0.0.1:8080/api/v1`. The address comes from `api.listen`.
- Send `Authorization: Bearer <token>` on `/api/v1` requests. The token is `api.token` from the
  config or, when that is empty, a random token created on first start. With `encryption.mode` set to
  `keystore` (the default), the bridge uses the OS keystore. A new file token in this mode requires
  `api.token_file_fallback`. The other modes use `store/api.token`. `wa-bridge api-token` prints it.
- The `Host` header must match the listen address (`127.0.0.1:8080`, `localhost:8080` or `[::1]:8080`).
  Other values get 403. This blocks DNS rebinding.
- Request bodies are JSON and at most 256 KiB. The exception is `POST /import`, which also takes a form upload up to `import.max_mb`.
- Responses are JSON objects with `"ok": true` or, for errors, `{"ok": false, "error": "..."}`.
- Timestamps in responses are unix seconds. Times in requests accept RFC 3339 (`2026-11-23T09:30:00Z`),
  a date (`2026-11-23`, local time) or unix seconds.
- `chat` and `to` accept a JID (`15550100002@s.whatsapp.net`, `120363000000000001@g.us`) or a phone number
  with country code.
- Every response has `Cache-Control: no-store`.

Set up a shell variable for the examples:

```bash
TOKEN=$(./wa-bridge api-token)
API=http://127.0.0.1:8080/api/v1
```

In Windows PowerShell use `curl.exe`, read the token with `$TOKEN = .\wa-bridge.exe api-token` and put
JSON bodies in a file passed with `-d "@body.json"`.

## Errors

| Status | Meaning |
| --- | --- |
| 400 | Invalid input, for example a bad JID, an unknown field value, a file of the wrong format or an attachment larger than `media.max_download_mb`. |
| 401 | Missing or wrong token. |
| 403 | Host header mismatch, sending disabled, quiet hours with action `refuse`, a file outside the allowed folders or inside the store, or `read_receipts.allow_mark_read` set to `false` for `/read`. |
| 404 | Message, entry, profile picture, import file or chat not found. |
| 405 | Method not allowed for this path. The `Allow` header lists the methods. |
| 409 | The same message went to this chat within the duplicate window, a scheduled entry is not pending, several chats have the title given to `/import`, or another import is running. |
| 413 | Request body larger than 256 KiB, or an import file or archive larger than `import.max_mb`. |
| 422 | The stored attachment details are incomplete. |
| 429 | A sending limit was reached. |
| 500 | A local problem in the bridge, for example a database error. |
| 502 | WhatsApp or its media servers answered with an error. |
| 503 | The bridge is not logged in or not connected. |

## Status

`GET /status`

`app_state` maps each observed collection to `synced`, `recovering` or `failed`. An empty map means
this session has seen no sync result yet. On a verification failure the bridge retries a full sync
up to three times for the affected collection. The bridge logs the recovery result once. Login and
logout lines identify the account with an ASCII mask such as `+1 *** *** 0101`.

```bash
curl -H "Authorization: Bearer $TOKEN" $API/status
```

```json
{
  "account": "+1 *** *** 0101",
  "connected": true,
  "logged_in": true,
  "ok": true,
  "protection": {"api_token": "dpapi", "compaction": "done", "efs": "on", "field_encryption": "keystore", "key_store": "dpapi", "plain_copy": "none"},
  "security_alert": true,
  "security_events": [
    {
      "at": 1795430400,
      "kind": "device_added",
      "level": "warning",
      "detail": "a new device (number 12) was linked to this account. If it is not yours, remove it under Linked devices on your phone"
    }
  ],
  "disconnect_reason": "",
  "webhook_dropped": 0,
  "sending_enabled": false,
  "version": "0.1.0"
}
```

`account` shows only the first digit and the last four digits of your number. It is empty before login.
`disconnect_reason` gives the reason for a permanent disconnect. `webhook_dropped` counts deliveries
lost to full webhook queues.

`security_events` lists the last 20 session events, newest first: `logged_out`, `session_replaced`
(another client connected with the keys of this session), `device_added` and `device_removed` (the
account's own device list changed), `account_mismatch` (another account tried to use this store) and
`paired`. `security_alert` is true while a warning event is less than 7 days old. `protection` says how
the store is protected; `compaction` is `pending` while old plain text may still be in the file after the
first encryption, and `plain_copy` is `kept` when the copy made before it could not be removed. See
[Security and privacy](../security.md#session-events).

### Health

`GET /health` at the root needs no token. It checks the Host header and returns `connected`, `logged_in`
and `ok`. A linked bridge with a permanent disconnect returns HTTP 503 with `ok: false` and
`permanent_disconnect: true`. Login preview returns HTTP 200 with `connected` and `logged_in` both false.
`wa-bridge health` exits 0 when the endpoint returns HTTP 200 with `ok: true`.

```bash
curl http://127.0.0.1:8080/health
```

## Login URL

`POST /login/url` issues a one-time link to the active login window. Send the bridge token and no body.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "$API/login/url"
```

On success, HTTP 200 returns `{"ok":true,"url":"http://127.0.0.1:49152/?t=..."}`. With
`login.public.url` set, the link uses that address. A link expires after 10 minutes. Issuing a sixth
unused link drops the oldest. HTTP 409 means there is no active login window, for example after linking.
`wa-bridge login-url` calls this endpoint.

## Chats

`GET /chats`

| Parameter | Meaning |
| --- | --- |
| `query` | Substring of the title or JID. |
| `limit` | Default 50, at most 500. |
| `offset` | Default 0. |

```bash
curl -H "Authorization: Bearer $TOKEN" "$API/chats?query=book&limit=5"
```

```json
{
  "chats": [
    {
      "jid": "120363000000000001@g.us",
      "title": "Book club",
      "is_group": true,
      "last_activity": 1795426198
    }
  ],
  "ok": true
}
```

## Messages

`GET /messages`

| Parameter | Meaning |
| --- | --- |
| `chat` | Chat JID or phone number. All chats when omitted. |
| `after`, `before` | Only messages sent after or before this time. |
| `query` | Case-insensitive text search in the body and the attachment name, in any script. Deleted messages are not searched unless `include_deleted` is set. |
| `order` | `desc` (newest first, default) or `asc`. |
| `include_deleted` | `true` shows the text of messages that were deleted for everyone. Without it they are listed with `deleted_at` set and an empty body. |
| `limit`, `offset` | As for chats. |

```bash
curl -H "Authorization: Bearer $TOKEN" "$API/messages?chat=15550100002&after=2026-11-01&query=invoice&limit=10"
```

```json
{
  "messages": [
    {
      "conv": "15550100002@s.whatsapp.net",
      "msg_id": "3EB0A1B2C3D4E5F6",
      "author": "15550100002@s.whatsapp.net",
      "outgoing": false,
      "sent_at": 1795426198,
      "body": "Can you send the new invoice today?",
      "kind": "text"
    }
  ],
  "ok": true
}
```

`kind` is `text`, `image`, `video`, `audio`, `voice`, `document`, `sticker`, `location`, `contact` or
`poll`. The last three have no attachment and carry a text summary in `body`. Optional fields:
`attachment_name`, `mime`, `byte_size`, `local_path`, `reply_to_id`, `reply_to_author`, `reply_preview`,
`mentioned`, `read_at`, `edited_at` (an edit by the author replaced the text) and `deleted_at`.

## Send

`POST /send/preview` accepts `text` and optional `mentions`. It returns the length including mention
tags and the exact ordered `parts` for approval. It sends no messages.

`POST /send`. Needs `sending.enabled`. See [Sending](../guides/sending.md) for the pipeline.

| Field | Meaning |
| --- | --- |
| `to` | Required. Chat JID or phone number. |
| `text` | Message text, or the caption of an image, video or document. |
| `media_path` | Existing local file inside `downloads_dir`, `exports_dir` or a folder in `sending.allowed_media_dirs`. Never a file of the store folder or the config file. |
| `as_voice` | Send `media_path` (Ogg/Opus) as a voice note. |
| `sticker_path` | Local `.webp` file sent as a sticker. |
| `reply_to` | `{"id": "<message id>"}`. `author` and `preview` are looked up when omitted. |
| `mentions` | JIDs or phone numbers. `@<number>` is added to the text when missing. |
| `human` | Default `true`. `false` skips the human-like waits. Limits and duplicate protection still apply. |

At least one of `text`, `media_path` and `sticker_path` is required.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to":"15550100002","text":"See you at the book club","reply_to":{"id":"3EB0A1B2C3D4E5F6"}}' \
  $API/send
```

```json
{
  "id": "3EB0F6E5D4C3B2A1",
  "ok": true,
  "sent_at": 1795426260,
  "waited_ms": 4210
}
```

`waited_ms` is the time spent on human-like waits. When one request becomes several messages, `ids` lists
them all. If a multipart send fails after sending some parts, the error includes `partial: true` and
`ids` for those already sent. Retrying the same request within the resume window skips those parts.
During quiet hours with action `queue` the answer is 202:

```json
{ "ok": true, "queued": true, "scheduled_id": 7 }
```

A send can take longer than a few seconds because of the waits. Use a client timeout of at least three
minutes.

## Message actions

These authenticated POST endpoints accept JSON with `chat` and `message_id` from the selected account's
synced archive. Each uses the `/send` pipeline and returns its sent (200) or quiet-hours queued (202)
response. The returned `id` identifies the action sent to WhatsApp. HTTP and SDK callers handle their
own approvals. MCP clients use the configured send confirmation flow.

| Endpoint | Additional JSON fields | Python SDK |
| --- | --- | --- |
| `/messages/react` | `emoji` is required. An empty string clears your reaction. | `bridge.react_to_message(chat, message_id, emoji)` |
| `/messages/edit` | `text` is the full replacement, up to 4096 characters. Requires your own text message within the edit window. | `bridge.edit_message(chat, message_id, text)` |
| `/messages/delete` | Requires your own message. Requests deletion for everyone. | `bridge.delete_message(chat, message_id)` |
| `/messages/forward` | `to` is the destination. Supply optional `expected_text` to require an exact match with a reviewed preview. Supports text up to 4096 characters. | `bridge.forward_message(chat, message_id, to, expected_text=preview)` |

An unknown source returns 404. Imported or deleted messages and unsupported content return 400.
Trying to edit or revoke someone else's message returns 403. A duplicate action or changed forward
preview returns 409. Rate limits return 429.

## Read receipts

`POST /read` marks messages as read on the account (all linked devices).

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"chat":"15550100002","ids":["3EB0A1B2C3D4E5F6"]}' $API/read
```

```json
{ "marked": 1, "ok": true }
```

`ids` takes 1 to 200 WhatsApp message IDs. Local import IDs return 400. In groups, `author` is the sender of the messages. It can be left out
when the messages are stored. The endpoint answers 403 when `read_receipts.allow_mark_read` is `false`.

## History

`POST /history` asks your phone for older messages. Nothing is sent to the contact.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"chat":"15550100002","count":50}' $API/history
```

```json
{
  "anchor_id": "3EB0A1B2C3D4E5F6",
  "note": "older messages arrive in the background if the phone is online",
  "ok": true,
  "oldest_known": 1795000000,
  "requested": 50
}
```

`count` is 1 to 100 (default 50). The request is anchored at `before_id` or at the oldest stored WhatsApp message in
the chat. Messages from local imports cannot anchor a request. The older messages arrive in the background and
are stored like any other history.

## Media

`POST /media/download` saves the attachment of a message to `downloads_dir/<chat>/`, where the folder name is the chat JID with `@` replaced by `_`.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"chat":"15550100002","id":"3EB0A1B2C3D4E5F6"}' $API/media/download
```

```json
{
  "attachment_name": "invoice.pdf",
  "kind": "document",
  "ok": true,
  "path": "/path/to/wa-mcp/downloads/15550100002_s.whatsapp.net/invoice_3EB0A1B2C3.pdf"
}
```

A document keeps its name plus the first characters of the message id, or only the message id when the
database is encrypted. When the WhatsApp copy has expired, the bridge asks the phone to upload the file
again, waits up to 40 seconds and retries. Attachments larger than `media.max_download_mb` (512 by
default) are refused with 400.

`GET /avatar?jid=<jid or phone>` saves the full-size profile picture to `downloads_dir/avatars/` and returns
`{"ok": true, "path": "..."}`. It answers 404 when there is no picture.

## Calls

Incoming calls are recorded when offered, accepted on another device, rejected, missed or ended. Calls placed from the phone are not visible live. A call can appear later if WhatsApp supplies a synced call-log record.

`GET /calls`, with `since` (time) and `limit` (default 50, maximum 500).

```json
{
  "calls": [
    {
      "call_id": "A1B2C3D4E5F60718",
      "peer": "15550100003@s.whatsapp.net",
      "outgoing": false,
      "video": false,
      "started_at": 1795420000,
      "outcome": "missed"
    }
  ],
  "ok": true
}
```

`outcome` is `missed`, `rejected`, `answered` or `unknown`. `group_jid` and `ended_at` appear when known.
Calls are recorded only when `calls.record` is true.

## Schedule

`GET /schedule` lists scheduled messages, soonest first. The optional `status` parameter is `pending`,
`sending`, `sent`, `failed` or `cancelled`.

`POST /schedule` creates one. Needs `sending.enabled`.

| Field | Meaning |
| --- | --- |
| `to` | Required. |
| `at` | Required. First run, RFC 3339 or unix seconds. At most 5 minutes in the past. |
| `text`, `media_path`, `sticker_path` | At least one is required. A file and a sticker cannot be combined in one entry. `media_path` and `sticker_path` follow the same folder rules as `/send`. |
| `as_voice` | Send `media_path` (Ogg/Opus) as a voice note. |
| `human` | `false` skips the human-like waits for this entry. When omitted, the config decides. |
| `reply_to` | A message id or `{"id": "..."}`. |
| `mentions` | As for `/send`. |
| `recurrence` | `once` (default), `daily`, `weekly` or `monthly`. |

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to":"120363000000000001@g.us","text":"Book club tonight at 19:00","at":"2026-12-03T08:00:00Z","recurrence":"once"}' \
  $API/schedule
```

The answer has status 201 and contains `id`, `next_run` and the stored entry in `scheduled`. The entry keeps
how its file is sent (media, voice note or sticker) and its `human` flag, so the later send matches the
request.

`DELETE /schedule/{id}` cancels a pending entry:

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" $API/schedule/7
```

```json
{ "cancelled": true, "id": 7, "ok": true }
```

It answers 409 when the entry is not pending.

## Import

`POST /import` stores the messages of a WhatsApp "Export chat" file (`.txt` or `.zip`) in one chat. It reads
files of the Android and iPhone layouts, copies the media of a zip to `downloads_dir/<chat>/` and skips
messages that are already stored. Nothing is sent to WhatsApp. See [Import an exported chat](../guides/import.md)
for the formats, the rules and the report.

Send either a form upload with the file in the field `file`, or JSON with `path`, a file inside
`downloads_dir`, `exports_dir` or a folder in `import.allowed_dirs`.

| Field | Meaning |
| --- | --- |
| `file` or `path` | The export. A path outside the allowed folders, a link or a store file gets 403. |
| `chat` | Required. JID, phone number or the exact title of a stored chat. |
| `me` | The sender name that is you, as the export shows it. Needed unless the push name or the file name tells. |
| `date_order` | `dmy`, `mdy` or `ymd`. Detected when omitted. |
| `timezone` | Zone name such as `Europe/Berlin`. Default is the local zone of the bridge. |
| `dry_run` | `true` reads and reports without writing. |
| `names` | Object that maps a sender name to a JID or phone number. In a form, a JSON string. |

```bash
curl -H "Authorization: Bearer $TOKEN" -F "file=@WhatsApp Chat with Alice.zip"   -F chat=15550100002 -F me=Bob -F dry_run=true $API/import
```

```json
{
  "chat": "15550100002@s.whatsapp.net",
  "date_order": "dmy",
  "date_order_source": "detected",
  "dry_run": true,
  "duplicates": 37,
  "first": "2023-02-11T08:15:00+01:00",
  "last": "2023-09-30T22:40:00+02:00",
  "media_added": 212,
  "messages_added": 1204,
  "ok": true,
  "skipped_lines": { "count": 0, "first": [] },
  "system_lines": 6,
  "timezone": "Europe/Berlin"
}
```

The report has more fields, listed in the guide. A missing chat answers 404 and several chats with the
title answer 409, both with `candidates`. A name for `me` that is not in the file, or no way to tell,
answers 400 with the names in `senders`. A file that is not a usable export, an unsafe archive or an
undecided date order answers 400. A big import can take minutes, so use a long client timeout. Imports run
one at a time.

## OpenAPI document

`GET /openapi.yaml` returns the OpenAPI 3.1 description that this page summarizes.

## Python

The same calls are available as methods of `wa_mcp.client.Bridge`, which reads the address and the token
from your config:

```python
from wa_mcp import config
from wa_mcp.client import Bridge

with Bridge(cfg=config.load("config.json")) as bridge:
    print(bridge.status())
    print(bridge.chats(query="book", limit=5))
```
