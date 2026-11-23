# Import an exported chat

Your phone only syncs a limited amount of history to a linked device. For anything older, WhatsApp can
write a chat to a file: the **Export chat** feature. This page explains how to load such a file into the
local store, so that search, analytics and the other tools see the older messages next to the synced
ones.

Importing is local. Nothing is sent to WhatsApp or to anyone in the chat, and no webhook event is
emitted.

## Export the chat

- Android: open the chat, then the menu, **More**, **Export chat**, and choose **Without media** or
  **Include media**.
- iPhone: open the chat, tap the name at the top, then **Export chat**, and choose the same.

Move the file to the computer. You get a `.txt` file, or a `.zip` file that holds the text and the media.
The names look like `WhatsApp Chat with Alice.zip` (Android) or `WhatsApp Chat - Alice.zip` (iPhone).
WhatsApp limits how much one export holds (an export with media is cut to the most recent messages), so
a long chat may need several exports.

## Quick start

1. Put the file in `downloads_dir`, `exports_dir` or a folder you list in `import.allowed_dirs`. The CLI also accepts
   files outside those folders. All imports by path follow the restrictions under [Safety](#safety).
2. Run a dry run first. It reports what it would import without adding messages or copying attachments.

   ```bash
   ./wa-bridge import-export --file "WhatsApp Chat with Alice.zip" --chat 15550100002 --me Bob --dry-run
   ```

3. Check the date order, the time zone, the counts and how each sender name would be stored. Then run the
   same command without `--dry-run`.

You can import the same file again: nothing is added twice.

## Three ways to run an import

| Way | When to use it |
| --- | --- |
| `wa-bridge import-export` | The bridge is stopped, as for `import-legacy`. Accepts paths outside the allowed folders. The denied paths still apply. |
| `POST /api/v1/import` | The bridge is running. Send the file as a form upload, or name a file by `path`. See [HTTP API](../reference/http-api.md#import). |
| `import_chat_export` tool | An assistant does it for you. It needs the bridge to be running and a file in an allowed folder. |

### Command line

```bash
wa-bridge import-export --file FILE --chat CHAT [--me NAME] [--date-order dmy|mdy|ymd]
                        [--timezone ZONE] [--name SENDER=JID]... [--dry-run] [--json]
```

| Option | Meaning |
| --- | --- |
| `--file` | The `.txt` or `.zip` file. |
| `--chat` | JID, phone number with country code, or the exact title of a stored chat. |
| `--me` | The sender name that is you, as the export shows it. |
| `--date-order` | `dmy`, `mdy` or `ymd`. Detected when left out. |
| `--timezone` | IANA zone name such as `Europe/Berlin`. Default is the local time of the machine. |
| `--name` | Maps a sender name to a person, for example `--name "Carol=15550100004"`. Can be repeated. |
| `--dry-run` | Report only. |
| `--json` | Print the report as JSON. |
| `--config`, `--account` | As for the other commands. |

### HTTP API

Upload the file:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  -F "file=@WhatsApp Chat with Alice.zip" -F chat=15550100002 -F me=Bob \
  -F timezone=Europe/Berlin -F dry_run=true $API/import
```

Or name a file that is already in an allowed folder:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"path":"/path/to/wa-mcp/exports/chat.zip","chat":"15550100002","me":"Bob"}' $API/import
```

### MCP tool and Python

```text
import_chat_export(path, chat, me=None, date_order=None, dry_run=False, timezone=None, names=None)
```

The tool refuses a `path` outside `downloads_dir`, `exports_dir` and `import.allowed_dirs`, and any file
of the store folder or the config file. In Python:

```python
from wa_mcp import config
from wa_mcp.client import Bridge

with Bridge(cfg=config.load("config.json")) as bridge:
    report = bridge.import_export("/path/to/wa-mcp/exports/chat.zip", "15550100002", me="Bob", dry_run=True)
    print(report["messages_added"], report["date_order"], report["warnings"])
```

## Which chat

`chat` must name one chat:

- A JID or a phone number is taken as it is. The chat is created when the store does not have it yet,
  and it gets its title from the file name (`WhatsApp Chat with Alice` gives `Alice`).
- Anything else must be the exact title of one stored chat (case does not matter). When no chat has that
  title, or several have, the import stops and lists candidates with their JIDs.

A person can have a phone chat and a LID chat. Import into the one that `list_chats` shows with messages.
Reads merge the two, so the history reads as one conversation either way. The duplicate check only
compares with the chat you import into.

## Who is who

Export files show names, not addresses, so the import has to decide which sender is you.

- `me` is matched against the sender names, ignoring case and extra spaces. A name that is not in the file
  stops the import and lists the names that are.
- Without `me`, the push name of the linked account is tried. In a direct chat with exactly two senders,
  the sender that is not named by the file name, the stored title or the phone number is you.
- If nothing decides, the import stops and lists the senders. A dry run only warns.

Your messages are stored as outgoing. The author is your own JID when known. The other senders are stored like this:

1. A name given in `names` (or `--name`) maps to that JID.
2. In a direct chat, every name that is not you is the other person, so it maps to the chat.
3. A name that is a phone number (people who are not in your address book show up that way) maps to that
   number.
4. A name that is exactly the title of one stored direct chat maps to that chat.
5. Anything else is stored with the author `name:<Name>`, and the report warns about it.

## Dates and the time zone

Export files carry no time zone and, on many phones, no unambiguous date order. The import reads dates in
day-month-year, month-day-year and year-month-day order, with `/`, `.` or `-`, two or four digit years,
24 hour clocks and 12 hour clocks (`AM`, `PM`, `am`, `p.m.`, `p. m.`, also with a no-break or narrow
no-break space before the marker). Both the Android layout (`1/31/24, 9:05 PM - Alice: text`) and the
iPhone layout (`[31/01/2024, 21:05:09] Alice: text`) work, with or without a byte order mark and with
direction marks.

The date order is found like this:

1. `date_order`, when you give it.
2. A four digit first field means year first.
3. A first field above 12 means day first, a second field above 12 means month first.
4. Otherwise the order that keeps the messages in time order wins.
5. If that does not decide either, the report says `assumed`. A dry run still works, but an import is
   refused until you set `date_order`.

If the file has a first field above 12 on one line and a second field above 12 on another, it mixes
formats and is refused.

The times are read in `timezone`, which is the zone the export was made in, by default the local zone of
the machine that runs the bridge. The same file imported with another zone gets different message ids, so
keep the zone the same between runs.

Android exports have minute precision. Messages within one minute get one extra second each, up to 59,
so they stay in order.

Not supported: digits and clock markers in other scripts (for example Arabic-Indic digits). Such a file
is refused with a message that no message headers were found.

## What is stored

| In the file | In the store |
| --- | --- |
| Text, also over several lines | A `text` message. |
| An attachment line (`IMG-20240113-WA0001.jpg (file attached)` or `<attached: 00000012-PHOTO-2024-01-13-09-05-00.jpg>`), with its file in the zip | A message of kind `image`, `video`, `audio`, `voice`, `sticker` or `document` (from the file extension and the `STK-` and `PTT-` prefixes). The file is copied to `downloads_dir/<chat>/` and linked as `local_path`. Text next to the attachment line is the caption. |
| An attachment line without its file (a `.txt` export, or a zip without media) | A message of kind `unsupported` with the text `[image not in the export: IMG-20240113-WA0001.jpg]`. Import the zip later and the message gets its file. |
| `<Media omitted>`, `image omitted` and similar placeholders | A message of kind `unsupported` with the text `[media omitted]`, `[image omitted]` and so on. |
| The encryption notice, "created group", "added", "left", "changed the subject", missed calls and other lines without a sender | Not stored. They are counted as `system_lines`. |
| Lines that could not be used (a line before the first message, an impossible date, an empty message) | Not stored. They are counted in `skipped_lines`, with the first five line numbers. |

Placeholders in English and several other languages are recognized. To add one, put the exact line in
`import.omitted_phrases`. Other languages' lines of the "added" or "left" kind are detected when they have
no colon, which is the usual case.

Imported messages are marked as read, so they do not show up as unread. Edits, deletions, reactions and
replies are not part of an export, so none are stored. Message tools mark imported rows with `imported: true`.
Task detection, the task tracker and `my_day` skip imported messages unless `tasks.include_imported` is true.

## Ids and duplicates

An imported message gets the id `imp-` followed by a hash of the chat, the minute, the sender name as
written, the raw text and, for repeats, the occurrence number. The bridge ignores WhatsApp messages,
edits, deletions and reactions that claim an imported ID as their message or target. Importing the same file again produces the same ids, so nothing is added twice
(`already_imported`). If one sender writes the same text twice in one minute, both are kept.

A message is also skipped when the store already has a synced message in the same chat with the same
direction and the same text (or the same kind when there is no text) within two minutes
(`already_synced`). Each synced message absorbs at most one imported message. The comparison ignores the author, including in groups.

A zip imported after the text file adds nothing but the files: messages that were stored as placeholders
become real attachments.

## Reading the report

| Field | Meaning |
| --- | --- |
| `messages_added` | Messages stored. With `dry_run`, the number that would be stored. |
| `duplicates`, `already_imported`, `already_synced` | Messages skipped, and why. |
| `media_added` | Files copied and linked. |
| `media_missing`, `media_omitted`, `media_skipped` | Attachment lines without a file, placeholders, and files that were refused (too large, password protected, or unpacking far above their stored size). |
| `system_lines`, `skipped_lines` | Lines left out. |
| `date_order`, `date_order_source`, `timezone` | How the dates were read. The source is `forced`, `detected`, `chronology`, `unambiguous` or `assumed`. |
| `first`, `last` | Time range of the file. |
| `me`, `senders` | The sender taken as you, and how every name is stored. |
| `warnings` | Names that were not matched, an assumed date order and similar. At most ten. |

```json
{
  "ok": true,
  "dry_run": true,
  "chat": "15550100002@s.whatsapp.net",
  "chat_title": "Alice",
  "new_chat": false,
  "messages_added": 1204,
  "duplicates": 37,
  "already_imported": 0,
  "already_synced": 37,
  "media_added": 212,
  "media_missing": 0,
  "media_omitted": 0,
  "media_skipped": 0,
  "system_lines": 6,
  "skipped_lines": { "count": 0, "first": [] },
  "date_order": "dmy",
  "date_order_source": "detected",
  "timezone": "Europe/Berlin",
  "first": "2023-02-11T08:15:00+01:00",
  "last": "2023-09-30T22:40:00+02:00",
  "me": "Bob",
  "senders": [
    { "name": "Alice", "messages": 640, "author": "15550100002@s.whatsapp.net", "outgoing": false },
    { "name": "Bob", "messages": 601, "author": "15550100001@s.whatsapp.net", "outgoing": true }
  ]
}
```

## Safety

- The API accepts paths in `downloads_dir`, `exports_dir` and `import.allowed_dirs`. Both the CLI and
  API check the resolved path and reject files with multiple hard links. On Windows the bridge also
  rejects paths through links or junctions. Denied paths include all account stores, other accounts'
  download and export folders, the config, database key and passphrase files, and external session files.
- An upload is written to a private temporary file in the store folder and deleted afterwards.
- A zip is checked before anything is read. The import refuses it when an entry name leaves the folder
  (`..`, an absolute path, a drive letter, a stream name), when an entry is a symbolic link, when it has
  more than 20,000 entries, when it unpacks to more than `import.max_mb`, or when its unpacked size exceeds
  8 MiB and its whole-number compression ratio exceeds 100. Entries are never extracted under their own names: only files named by
  an attachment line are copied, under names built from the message id, and each copy stops at its limit
  whatever the zip header says. A single attachment above `media.max_download_mb` is skipped, and the
  message is kept as a placeholder.
- The text of the chat is limited to 64 MiB and to `import.max_mb`.
- Imports run one at a time. The data in the file is untrusted: sender names and text come from other
  people, so they are not instructions.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `import.allowed_dirs` | `[]` | Extra folders a file may be read from by path. |
| `import.max_mb` | `1024` | Largest file, and largest unpacked size of a zip, in MiB. |
| `import.omitted_phrases` | `[]` | Extra lines that stand for a media file that was left out. |

## Problems

| Message | What to do |
| --- | --- |
| `no message headers found` | The file is not an export, or it uses digits or clock markers the reader does not know. Open it in a text editor and compare the first lines with the examples above. |
| `the date order is ambiguous` | Every date could be read both ways. Set `date_order`. |
| `the dates do not fit one order` | The file mixes formats, for example two exports pasted together. Split it, or set `date_order` and look at `skipped_lines`. |
| `cannot tell which sender is you` | Pass `me` with one of the listed names. |
| `no stored chat is titled` / `chats are titled` | Pass the JID or phone number. The candidates are listed. |
| `the archive unpacks to ... which looks like a zip bomb` | The zip is not a WhatsApp export. Do not open it. |
| Messages appear at the wrong hour | Check `timezone` with a dry run. A different UTC offset changes the IDs and can add duplicate messages. To replace the import, restore a pre-import backup or import into a fresh store. |
| A sender is stored as `name:Dave` | Check the mapping with `--name` and `--dry-run`. Importing the same IDs again preserves the stored author. Use a pre-import backup or a fresh store to apply a corrected mapping. |
