# 7. Import old exports

Goal: load a WhatsApp "Export chat" file into the local store, so that search, analytics and the other tools
see history that is older than what the phone syncs. Importing is local. Nothing is sent to WhatsApp or to
anyone in the chat. This takes about fifteen minutes.

## Prerequisites

- The bridge is built, see [Install](01-install.md). The command line import needs the bridge to be stopped.
  Importing through an assistant or the API needs it to run.
- The chat that you want to export, on your phone.

## Steps

### 1. Export the chat on the phone

- Android: open the chat, then the menu, **More**, **Export chat**, and choose **Without media** or
  **Include media**.
- iPhone: open the chat, tap the name at the top, then **Export chat**, and choose the same.

Move the file to your computer. You get a `.txt` file, or a `.zip` file with the text and the media. The
names look like `WhatsApp Chat with Alice.zip` (Android) or `WhatsApp Chat - Alice.zip` (iPhone). WhatsApp
limits what one export holds, so a long chat may need several exports.

### 2. Put the file in the exports folder

Copy the file into the `exports` folder of the repository (`exports_dir` in `config.json`). The assistant
and the API only read files from `downloads_dir`, `exports_dir` and the folders in `import.allowed_dirs`.
The command line accepts any folder except the store folder and the config file.

### 3. Practice with a tiny file

A dry run reads everything, writes nothing and reports what it found. Make a practice file first.

macOS and Linux:

```bash
mkdir -p exports
cat > "exports/WhatsApp Chat with Alice.txt" <<'EOF'
13/01/2024, 09:00 - Alice: Good morning
13/01/2024, 09:01 - Bob: Morning
14/01/2024, 18:30 - Alice: See you tomorrow
EOF
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force exports | Out-Null
@'
13/01/2024, 09:00 - Alice: Good morning
13/01/2024, 09:01 - Bob: Morning
14/01/2024, 18:30 - Alice: See you tomorrow
'@ | Set-Content -Encoding utf8 "exports\WhatsApp Chat with Alice.txt"
```

Stop the bridge with Ctrl+C, then run a dry run. `--chat` is a phone number with country code or a JID,
and `--me` is the sender name that is you, as the export shows it.

macOS and Linux:

```bash
./wa-bridge import-export --file "exports/WhatsApp Chat with Alice.txt" --chat 15550100002 --me Bob --dry-run --json
```

Windows PowerShell:

```powershell
.\wa-bridge.exe import-export --file "exports\WhatsApp Chat with Alice.txt" --chat 15550100002 --me Bob --dry-run --json
```

You should see a report with `"dry_run": true` and `"messages_added": 3`, the detected `date_order` (`dmy`
here, because the first field is 13), the `timezone`, and the senders with how each one would be stored.
Nothing was written. Remove the practice file, or use a separate config with its own `store_dir`, when you
do not want a chat called Alice in your store.

### 4. Dry run your real export

```bash
./wa-bridge import-export --file "exports/WhatsApp Chat with Alice.zip" --chat 15550100002 --me Bob --dry-run
```

Check four things in the report:

1. `date_order` and `date_order_source`. If the source is `assumed`, add `--date-order dmy` or `mdy`.
2. `timezone`. It is the zone the export was made in, by default your local zone. Add
   `--timezone Europe/Berlin` or your zone if the phone used another one.
3. `messages_added` against `duplicates`. Messages that are already stored are skipped.
4. `senders`. Every other name should map to the chat. A name that does not match stays as `name:<Name>` and
   produces a warning. Fix it with `--name "Carol=15550100004"`.

`--chat` takes the JID, the phone number with country code, or the exact title of a stored chat. A person
can have a phone chat and a LID chat. Import into the one that `list_chats` shows with messages.

### 5. Run the import

Run the same command without `--dry-run`:

```bash
./wa-bridge import-export --file "exports/WhatsApp Chat with Alice.zip" --chat 15550100002 --me Bob
```

You should see the number of messages added. Media in a zip is copied to `downloads_dir/<chat>/`. You can
run the same import again. Nothing is added twice.

### 6. Look at the result

Start the bridge again, then ask the assistant: "Show the oldest messages in my chat with Alice." You should
see the imported messages, marked as imported, next to the synced ones. Imported messages are marked as read.

### 7. Import through the assistant (optional)

With the bridge running, ask: "Run a dry run of importing exports/WhatsApp Chat with Alice.zip into my chat with
Alice, I am Bob." The assistant calls `import_chat_export` with `dry_run=true`. Ask it to run the real import
after you have read the report. The file must be in an allowed folder.

### 8. Clean up

The export file and the media inside the zip stay as plain files where you put them. Delete or move them
once the import is done.

## Troubleshooting

- `no message headers found`. The file is not an export, or it uses digits or clock markers that the reader
  does not know. Open it in a text editor and compare the first lines with the practice file.
- `the date order is ambiguous`. Every date could be read both ways. Pass `--date-order dmy` or `mdy`.
- `cannot tell which sender is you`. Pass `--me` with one of the names that the error lists.
- `no stored chat is titled`. Pass the phone number or the JID instead of a title.
- `another bridge is using ...`. Stop the running bridge before the command line import.
- Messages appear at the wrong hour. The timezone was wrong. The messages of the first run stay, because
  their ids differ, so see [Import an exported chat](../guides/import.md) for how to remove them before
  you import again with the right `--timezone`.

## Next steps

- [Import an exported chat](../guides/import.md): formats, safety limits and every field of the report.
- [Media and screenshots](06-media-and-screenshots.md): read the text in imported images.
- [Several accounts](09-multiple-accounts.md): import into the right number's store with `--account`.
