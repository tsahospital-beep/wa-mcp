# End-to-end check with a real account

The unit tests and the CI jobs use fake data and never connect to WhatsApp. This page describes the check
that does: a script that you run by hand against a real test account, step by step, and that writes a
report with evidence. It is not part of CI, and nobody should run it against an account that matters.

The script is [`scripts/e2e_check.py`](../../scripts/e2e_check.py). The result of a run is a Markdown file
that you can read, keep and attach to an issue. Phone numbers and tokens are masked in it.

## What you need

- A WhatsApp account that you can lose. A second SIM or a spare number is best. Automated use of a
  WhatsApp account may go against the WhatsApp terms of service, and WhatsApp can limit or block it.
- The phone of that account, to scan codes, send a voice note and make a call.
- A test group with the account and at least one other member, for the group steps. A group of you and a
  friend is enough. Without it the group steps are skipped.
- A bridge that is running and linked. Use a store folder that you only use for this account, so that you can
  delete it afterwards.
- `sending.enabled` set to `true` in `config.json` for the send steps. Without it they are skipped.
- For the voice and screenshot steps: ffmpeg and a transcription backend, and an OCR engine. See
  [Voice transcription](../guides/transcription.md) and [Text from photos and screenshots](../guides/images.md).

Run everything from the repository root, after `uv sync`. Both commands below use the same `config.json`
as the bridge.

## What the script sends

It sends only to two places: your own number (the "message yourself" chat) and the test group that you
name with `--group`. It never sends to another chat. It asks before it acts:

- Before a step that other people can see (a group message, a read receipt) it prints the recipient and the
  exact action and waits until you type `send`. Any other answer skips the step.
- Before the steps that send to your own number it asks once, and you type `yes`.
- A step that only changes local state (the HTTP server step creates and revokes a token) asks the same way.

`--dry-run` prints the plan and does nothing else: it opens no connection, writes no file and starts no
process.

```bash
uv run python scripts/e2e_check.py --dry-run
```

## Run it

```bash
uv run python scripts/e2e_check.py --self-number 15550100001 --group "Test group"
```

In Windows PowerShell the command is the same. Replace the number with the number of the test account, with
the country code and digits only. The script compares it with the masked number that the bridge reports
(first digit and last four digits) and stops when they differ, so that a typo cannot send to a stranger.

| Option | Meaning |
| --- | --- |
| `--config FILE` | The config file. Default: `WA_MCP_CONFIG`, then `./config.json`. |
| `--self-number NUMBER` | The number of the test account. Asked when missing. |
| `--group JID_OR_TITLE` | The test group, as a JID or an exact title. Without it the group steps are skipped. |
| `--mention NUMBER` | The member to mention in the group reply. Default: the test account. |
| `--spoken WORDS` | The words you say in the voice note. Asked when missing. |
| `--image-word WORD` | A word that is printed in the screenshot. Asked when missing. |
| `--only IDS`, `--skip IDS` | Comma separated step ids to run or to leave out. |
| `--wait-seconds N` | How long to wait for things that you do by hand. Default 180. |
| `--list` | Print the step ids and exit. |
| `--dry-run` | Print the plan and touch nothing. |

The paths and ports come from the config: the bridge API address, the store, downloads and exports folders.
The report goes to `exports_dir/e2e/<run id>/report.md`. The same folder has a `screenshots` subfolder.

Drop screenshots there while the steps run and name them after the step, for example `login-qr.png` or
`reply_group-mention.png`. The report lists every file in the folder with its size and a checksum, and
never reads the content. The script writes the report again after each step, so a crash or Ctrl+C still
leaves the steps that were done.

Ctrl+C while the script waits for something that you do by hand stops the waiting. Ctrl+C at a prompt
stops the run.

## The steps

Each step ends as `PASS`, `FAIL` or `SKIPPED`, with evidence: timestamps, message ids, status codes, file
names and the answers you gave. A step is skipped, not failed, when it does not apply: sending is off, no
test group is named, Docker is not installed, you declined a confirmation.

| Id | What it checks | What you do | Visible to others |
| --- | --- | --- | --- |
| `preflight` | The bridge answers, is connected and logged in, and the number you gave fits the account. | Nothing. | No |
| `login` | The login window flow: QR code, expired code and reload, pairing by phone number, the window closing after Connected. | Do the login test by hand first (see [First login](../tutorials/02-first-login.md)) and answer four questions. | No |
| `chats` | Chats are listed, history is newest first, the second page does not repeat the first, oldest first works. | Nothing. | No |
| `send_self` | A send to your own number with human-like pacing: the reported wait fits the typing speed and jitter of your config, a send with `human=false` does not wait, the same text again is refused with 409, and the message is stored as outgoing. | Type `yes`. | No |
| `search` | The sent message is found by a Greek word in lowercase and in uppercase, and by the run marker, through the API and through the `list_messages` tool. | Nothing. | No |
| `media` | An image and a voice note that you send to your own chat can be downloaded: the file exists, is not empty and has the right file type. | Send a screenshot with printed text and a voice note to yourself from the phone. | No |
| `transcribe` | `transcribe_chat_audio` stores a transcript, and the words you say are in it. | Type the words you said. | No |
| `ocr` | `read_images` reads the screenshot, the word you typed is in the text, and search finds the image by that word. | Type a word from the screenshot. | No |
| `import` | A generated export file is imported into a local chat for the fictional number 15550100999: the dry run reports 6 messages, the import stores 6, a second import stores none, and the Greek word is found. Nothing is sent. | Nothing. | No |
| `reply_group` | A message to the test group, then a reply that quotes it and mentions a member. The stored reply points at the first message and keeps the mention. | Type `send`. Check the quote and the mention in WhatsApp and take a screenshot. | Yes |
| `mark_read` | One incoming group message is marked as read and the bridge reports it. | Ask another member to post a message if there is none. Type `send`. | Yes |
| `schedule` | A message scheduled 2 minutes ahead is sent within 2 minutes of its time and is stored. A second entry is scheduled and cancelled. | Type `yes`. Wait up to 5 minutes. | No |
| `calls` | A test call appears in the call log through the API and through `list_calls`. | Make a short call, or reject an incoming one. | Yes, by your own action |
| `webhook` | A receiver the script starts on a random port gets a signed `message` delivery for a message the script sends to you. A forged signature is refused with 401. | Add the printed webhook entry to `config.json` and restart the bridge. Type `yes`. | No |
| `http_mcp` | `wa-mcp serve --http` on a random loopback port: no token and a wrong token get 401, a new token can initialize, list tools and call `bridge_status`, and the token stops working after it is revoked. | Type `yes`. | No |
| `docker` | Optional. The containers of the `wa-mcp` Compose project are healthy, have a read-only root file system, and the health checks answer. | Run the script on the Docker host. | No |
| `encryption` | Optional. A read-only snapshot of `wa.db` has sealed message bodies and none of the test text in plain form, and it cannot be opened with another key. The snapshot is deleted. | Nothing. | No |
| `accounts` | Optional. With an `accounts` block: `list_accounts` matches the config, stores and ports differ, and every bridge answers. | Nothing. | No |

The steps run in this order. Later steps use what earlier ones made: `search` needs the message from
`send_self`, and `transcribe` and `ocr` need what you sent in `media`. A step that misses its input is
skipped and says why. If `preflight` fails, the run stops, because nothing else can work.

### The webhook step

The bridge reads `webhooks` from `config.json` when it starts, so this step needs a restart. The script
starts [`examples/webhook_receiver.py`](../../examples/webhook_receiver.py) on a port that the system picks,
prints the entry to add, for example:

```json
{ "url": "http://127.0.0.1:50321/hook", "events": ["message"], "secret_env": "WA_MCP_WEBHOOK_SECRET" }
```

and, if the variable is not set in your shell, a new secret and the commands to set it in PowerShell and in
bash. Set the secret in the shell that starts the bridge, restart the bridge, wait until it is connected and
press Enter. The script stops the receiver by its process ID when the step ends. Remove the entry from
`config.json` afterwards and restart the bridge.

### The encryption step

The script copies the database with the SQLite backup call from a read-only connection, looks at the copy,
and deletes it. It checks that message bodies start with the sealed prefix, that the test text of this run
does not appear in the copied file, and that the copy cannot be opened without the right key. For the
`keystore` mode the copy sits in a folder without the key, so the keystore has nothing for it. For
`passphrase` the script tries a wrong passphrase, and for `keyfile` a new random key file. The step is
skipped when `encryption.mode` is `off`, and it fails when the mode is on but the database is plain.

## The report

```text
# WA MCP end-to-end report 20261123-101500

Result: **PASSED with skipped steps** (14 passed, 0 failed, 4 skipped)

- Started: 2026-11-23T10:15:00
- Finished: 2026-11-23T10:41:12
- Machine: Windows 11, Python 3.13.1
- Encryption mode: keystore

## Summary

| Step | Result | Seconds | Note |
| --- | --- | --- | --- |
| preflight | PASS | 0.2 |  |
| send_self | PASS | 21.4 |  |
| docker | SKIPPED | 0.0 | docker is not installed on this machine |
```

Each step then has its own section with a list of evidence, for example:

```text
## send_self: Send to yourself with human-like timing

Result: **PASS**. Started 10:16:02.

- message id: 3EB0A1B2C3D4E5F6
- wall time for the human-like send: 9.4 s
- waited_ms reported by the bridge: 8870
- expected wait window for this text: 4.6 to 15.7 s
- send with human=false: 0.3 s, waited_ms=0
- repeat of the first text: refused with status 409
- stored as outgoing: True
```

The last section lists the files that you dropped into the run folder.

## What a failure means

Read the note and the evidence of the failed step first. Most failures have a cause outside the code: a
missing ffmpeg or OCR engine, a bridge that was disconnected, a phone that was offline, an expired media
file, or a time limit that you hit. Run the step again with `--only` once the cause is fixed. If the
behavior is wrong, open an issue and attach the report. It has no message text except the test messages that
the script wrote itself.

## Clean up

- The `import` step leaves a local chat for the fictional number 15550100999 with 6 messages. Delete the
  test store folder when you are done, or leave the chat in place.
- The `webhook` step needs the entry in `config.json` removed again, followed by a bridge restart.
- The `http_mcp` step revokes its token. If the script was stopped in the middle, run `wa-mcp token list`
  and revoke any token whose name starts with `e2e-`.
- The `media` step saves two files into `downloads_dir`.
- Messages that the script sent stay in the chats. The test messages all contain `wa-mcp-e2e-` and the run id.
- To end the test, remove the device under Linked devices on the phone.

## What the script does not cover

It does not run in CI and does not replace the unit tests. It cannot tell how WhatsApp treats an account
that sends automated messages. It runs on one machine: the Docker step only looks at containers on that
host, and the container images themselves are built and smoke-tested in CI. Steps that need two people,
such as reading receipts as the sender sees them, need a second phone and your own eyes.
