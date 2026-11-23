# Sending

Sending is off until you turn it on. When it is on, every message still goes through a short pipeline
that checks recipients, files, limits, duplicates and quiet hours, and then paces the send like a person
would. This page explains the switches and the settings.

## Enable sending

Set `sending.enabled` to `true` in `config.json` and restart the bridge:

```json
{
  "sending": { "enabled": true }
}
```

You can also set the environment variable `WA_MCP_SENDING_ENABLED=true` for the bridge and the MCP server.
The bridge log shows `sending: enabled (limits ...)` when it is on.

With sending off, the send tools answer `sending is disabled`, the API returns 403 for `/send` and
`/schedule` and scheduled messages stay pending.

## Who approves a send

`sending.require_confirm` is `true` by default. The MCP send tools (`send_message`, `send_file`,
`send_voice_note`, `send_sticker`, `schedule_message`) then ask for approval before anything goes out:

- When the MCP client supports elicitation (a question from the server to the user), the server asks you
  directly. The request shows the recipient with the address and where the name comes from (address book,
  push name, group title), the full text (one message is at most 4096 characters), the people mentioned,
  the quoted message and the file name. Names chosen by other people are shown on one line without control
  characters. The question goes to you through the client, not to the model.
- When the client cannot ask, nothing is sent unless you set `sending.allow_confirm_flag` to `true`. With
  it, the tool accepts a `confirm=true` argument and tells the model to show you the exact recipient and
  content first. This is a weaker check, because the model sets the flag itself: a model that ignores the
  instruction, or one that is being steered by text in a message, can pass `confirm=true` without asking
  you. The server cannot tell the difference.

Check whether your client asks: with sending on, the first `send_message` call should show a question
before it returns.

With `require_confirm` set to `false` there is no approval step. `mark_as_read` and `cancel_scheduled` do
not ask either, but they carry a hint for clients that this tool changes data, so some clients ask by
themselves.

The HTTP API has no approval step. Anyone with the token can send once sending is enabled. Keep sending
off when you do not need it, and use the limits and the folder list below.

## Recipients

The send tools accept these targets:

- a JID, for example `15550100002@s.whatsapp.net` or `120363000000000001@g.us`,
- a phone number with country code (digits, with optional `+`, spaces or dashes),
- a name from `people.aliases`,
- the exact name of a contact in your address book, full or first name.

Groups can only be addressed by their JID. Push names (names that people choose for themselves) and chat
titles are never used to pick a recipient, because anyone can set them to look like someone else. If a
name matches several people, the tool answers with the candidates and sends nothing. Every result contains
`to` (the address) and `to_name` (the name shown to you), so you can see where the message went.

## Files

Files sent by `send_file`, `send_voice_note`, `send_sticker`, `schedule_message` and the API must be
regular files inside `downloads_dir`, `exports_dir` or a folder listed in `sending.allowed_media_dirs`.
The two default folders are always allowed; the list adds more. Paths are compared after resolving
symbolic links, and the comparison ignores case on Windows and macOS.

Files inside `store_dir` and the config file are never sent, whatever the list says, and the check
also catches other names for them: a file with more than one hard link is refused, and on Windows a
path through a junction or symbolic link is refused. An assistant that tries to send the WhatsApp
session file or the database is refused. The bridge opens the checked file once and uploads from that
same handle, so the file cannot be swapped between the check and the upload.

Voice notes need Ogg/Opus and stickers need WebP. The MCP tools convert other formats with ffmpeg: audio
to Ogg/Opus and PNG or JPEG to a 512x512 WebP. The converted file goes into a new folder under
`exports_dir/outgoing/`, which is removed after the send. Files are streamed to WhatsApp, not read into
memory; voice notes (read to find their length) are limited to 64 MiB and other files to 2 GiB.

## What the tools can send

| Tool | Sends |
| --- | --- |
| `send_message` | Text, optionally as a reply to a message id and with mentions. |
| `send_file` | An image, video or document with an optional caption. |
| `send_voice_note` | An audio file as a voice note. |
| `send_sticker` | A `.webp` file as is, or a converted image. |
| `schedule_message` | Text or a file at a later time. See below. |

- `reply_to` is the id of the message to quote. The bridge looks up the author and the quoted text in its
  database, so quote a message that it has stored.
- `mentions` are JIDs or phone numbers. The bridge adds `@<number>` to the text when it is missing, which
  is how WhatsApp highlights the name.
- A voice note or a sticker cannot carry text. Through the HTTP API, text sent together with one goes first
  as its own message, and the answer lists every message id in `ids`.

## Human-like sending

With `sending.human_like` on (the default), the bridge paces each send. This makes automated sending look
less mechanical. It does not guarantee anything about how WhatsApp treats an account.

For one text message the bridge does this, in order:

1. Checks the recipient, the file paths and the mentions. Then, in one step, it checks the limits and the
   duplicate window and reserves its place in the limits, so parallel sends cannot go over them together.
   A reservation for a message that was not sent is given back. Quiet hours are checked next. A refusal
   ends here.
2. Marks the latest unread incoming messages of the chat as read, when `sending.mark_read_before_reply`
   is on (up to 20 messages).
3. If the bridge sent something to this chat a moment ago, waits until a random pause from
   `sending.pause_between_messages_seconds` has passed.
4. Waits a random 0.4 to 1.5 seconds, as if reading.
5. Shows the typing indicator for `characters / typing_chars_per_second` seconds, kept between
   `typing_min_seconds` and `typing_max_seconds` and varied by plus or minus `jitter`.
6. Stops the indicator and sends.

A voice note shows a recording indicator for the length of the audio, within the same minimum and
maximum. A sticker uses the minimum. When one request becomes several messages, the pause from step 3
is applied between them.

With the defaults a very short reply shows typing for about 1.2 seconds and a 100-character message for
about 14 seconds, each varied by up to 30 percent. The answer of the API contains `waited_ms`, the time
spent on these waits.

| Key | Default | Effect |
| --- | --- | --- |
| `sending.human_like` | `true` | Turns the waits on or off. Limits, duplicate protection and quiet hours stay active when off. |
| `sending.mark_read_before_reply` | `true` | Mark the chat read before replying. |
| `sending.typing_chars_per_second` | `7` | Typing speed. |
| `sending.typing_min_seconds` | `1.2` | Shortest indicator. |
| `sending.typing_max_seconds` | `20` | Longest indicator. |
| `sending.jitter` | `0.3` | Random variation. |
| `sending.pause_between_messages_seconds` | `[1.0, 3.5]` | Pause between messages to one chat. |

Through the HTTP API a single request can skip the waits with `"human": false`. Limits and duplicate
protection still apply. The MCP tools always use the configured behavior.

## Limits and duplicates

| Key | Default | Effect |
| --- | --- | --- |
| `sending.limits.per_chat_per_hour` | `20` | Maximum messages to one chat in the last hour. |
| `sending.limits.per_hour` | `60` | Maximum messages to all chats in the last hour. |
| `sending.limits.per_day` | `300` | Maximum messages to all chats in the last 24 hours. |
| `sending.dedupe_window_minutes` | `10` | The same text or file to the same chat is refused within this window. |

Set a limit to `0` for no limit. Each part of a multi-part send counts. A reached limit returns HTTP 429
with `send limit reached: per_hour=60`. A duplicate returns HTTP 409. The counters are kept in the
database, so they survive a restart. The duplicate check stores a keyed hash of the message, not its text,
and the log of sent messages is pruned after two days.

## Quiet hours

Quiet hours stop sends during a daily window, for example at night.

```json
{
  "sending": {
    "quiet_hours": { "enabled": true, "start": "22:00", "end": "08:00", "action": "queue" }
  }
}
```

The times are the local time of the machine running the bridge. The window can cross midnight. With
`"action": "queue"` a message sent during the window is stored as a scheduled message for the end of the
window. It keeps its kind (text, file, voice note or sticker) and its `human` flag. The API answers 202
with `"queued": true` and a `scheduled_id`, and the MCP tool reports `queued=true`. With
`"action": "refuse"` the send fails with HTTP 403.

Scheduled messages are not held back by quiet hours, because you chose their time.

## Scheduled messages

`schedule_message` stores a message that the bridge sends later, even when no assistant is running.

- `at` is an ISO 8601 time or unix seconds. A time up to 5 minutes in the past is accepted. Anything older
  is refused.
- `recurrence` is `once`, `daily`, `weekly` or `monthly`. A monthly message keeps its day of the month and
  uses the last day in shorter months.
- The bridge checks for due messages every 15 seconds. It sends only while it is running, connected and
  sending is enabled. Runs missed while the bridge was off are sent once, not once per missed run.
- Scheduled sends use the normal pipeline, so limits, duplicate protection and human-like waits apply.
- A failed send is tried up to 5 times in total, with waits of 1, 5, 15 and 30 minutes between the
  attempts. After the fifth failure a one-time message is marked `failed`. A recurring message skips that
  run and keeps its schedule. Errors that cannot go away are not retried: a missing file, a file outside
  the allowed folders, an unknown recipient, a duplicate and a file that is too large or in the wrong
  format.
- If the bridge stops while a message is being sent, the next start handles it. A one-time message is
  marked `failed`, because it may already have gone out and a second send would be a duplicate. A recurring
  message skips the interrupted run and moves to its next run.
- Through the API an entry can also be a voice note (`as_voice`), a sticker (`sticker_path`) and carry its
  own `human` flag. The entry stores this, so the later send matches the request. The MCP tool schedules
  text or a plain file.

Status values are `pending`, `sending`, `sent`, `failed` and `cancelled`. List them with `list_scheduled`
and cancel a pending one with `cancel_scheduled`. The same is available over the
[HTTP API](../reference/http-api.md#schedule).

## Read receipts

Reading through an assistant does not mark messages as read. Use the `mark_as_read` tool when you want blue
ticks. Three settings change the default:

- `read_receipts.auto_mark_read` marks every new incoming message as read when it arrives.
- `sending.mark_read_before_reply` marks a chat read just before a reply goes out.
- `read_receipts.allow_mark_read` set to `false` blocks `mark_as_read` and the `/read` endpoint.

## Python

```python
from wa_mcp import config
from wa_mcp.client import Bridge

cfg = config.load("config.json")
with Bridge(cfg=cfg) as bridge:
    bridge.send("15550100002", text="See you at the book club", human=True)
    bridge.schedule("15550100002", "2026-12-01T09:00:00+00:00", text="Happy birthday", recurrence="once")
```

The SDK does not ask for approval. It only needs `sending.enabled`.
