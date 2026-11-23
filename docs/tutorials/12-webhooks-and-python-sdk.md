# 12. Webhooks and the Python SDK

Goal: receive webhook events from the bridge and check their signature, read your chats from Python, and
call the HTTP API. This takes about twenty minutes.

## Prerequisites

- The bridge is linked and running, see [First login](02-first-login.md).
- The Python packages are installed with `uv sync`, see [Install](01-install.md).
- You run the commands from the repository folder. The programs used here are in [`examples/`](../../examples/README.md).

## Steps

### 1. Make a secret

Every webhook request carries an HMAC-SHA256 signature made with a secret that only you and the bridge
know. Make a long random one and keep it in an environment variable. Use the same shell for the next steps,
and later for the bridge.

macOS and Linux:

```bash
export WA_MCP_WEBHOOK_SECRET="$(openssl rand -hex 32)"
```

Windows PowerShell:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$env:WA_MCP_WEBHOOK_SECRET = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
```

You should see no output. `echo $WA_MCP_WEBHOOK_SECRET` (`$env:WA_MCP_WEBHOOK_SECRET` in PowerShell) prints
64 hex characters.

### 2. Start the receiver

```bash
uv run python examples/webhook_receiver.py --port 9000
```

You should see one line, for example:

```text
listening on http://127.0.0.1:9000/hook (pid 4120)
```

The receiver accepts a delivery only when the signature matches, the timestamp is within 5 minutes of your
clock, and the delivery id is new. It prints one line per accepted event and never prints message text,
unless you add `--show-body`. Leave it running.

### 3. Tell the bridge where to send events

Add an entry to the `webhooks` list in `config.json`:

```json
{
  "webhooks": [
    {
      "url": "http://127.0.0.1:9000/hook",
      "events": ["message", "call"],
      "secret_env": "WA_MCP_WEBHOOK_SECRET"
    }
  ]
}
```

`events` can be any of `message`, `receipt`, `call`, `connection`, `scheduled` and `security`. An empty
list means all of them. Stop the bridge, and start it again from the shell that has the secret set, so that
the bridge process sees the variable:

```bash
./wa-bridge
```

You should see the log line `webhooks: 1 configured`. If the secret is missing the bridge still sends, but
without a signature, and logs a warning. The receiver then refuses every delivery.

### 4. Trigger an event

Send a message to your own number from the phone, or ask someone to write to you. You should see a line in
the receiver terminal such as:

```text
10:02:11 message conv=1***0001@s.whatsapp.net kind=text id=3EB0A1B2C3D4E5F6
```

Messages that arrive through history sync are not sent as events. Attachments are not part of the payload.
Download them with the `media/download` endpoint or the `download_media` tool.

### 5. Check that forged requests are refused

A request with a wrong signature must get 401. Send one by hand:

```bash
curl -i -X POST http://127.0.0.1:9000/hook -H "X-WA-MCP-Timestamp: 1" -H "X-WA-MCP-Delivery: test" -H "X-WA-MCP-Signature: sha256=00" -d "{}"
```

You should see `HTTP/1.0 401`. On Windows PowerShell use `curl.exe`. The signed text is
`<timestamp>.<delivery id>.<raw body>`, so a recorded delivery cannot be sent again later: the old
timestamp is refused, and a repeated delivery id is acknowledged and skipped. The bridge retries a failed
delivery with the same id after 1, 5 and 25 seconds, then drops it. Events are not stored, so events that
arrive while the receiver is down or the bridge is stopped are lost. Use `list_messages` to catch up.

### 6. Read your chats with the Python SDK

```bash
uv run python examples/python_sdk_basic.py
```

You should see the status of the bridge, your newest chats and the latest messages of the first one:

```text
bridge 0.1.0: connected=True logged_in=True account=+1 *** *** 0001

Chats (5):
  2026-11-23 09:41  direct  Alice
  2026-11-23 08:15  group   Book club

Last 5 messages, newest first:
  2026-11-23 09:41  them  Can you send the new invoice today?
```

Options: `--chats 10`, `--chat 15550100002`, `--messages 20` and `--download`, which saves the newest attachment
of the chat and prints its path. The script reads through the bridge API, so it works with an encrypted
store. To read the local database directly, use the `db` module:

```python
from wa_mcp import config, db, people

cfg = config.load("config.json")
db.connect(cfg)
people.connect(cfg)

for m in db.messages(after="2026-11-01", query="invoice", limit=5):
    print(m["time"], people.name_of(m["conv"]), m["body"][:60])
```

### 7. Call the HTTP API

The API listens on `127.0.0.1:8080` and every request needs the token.

macOS and Linux:

```bash
TOKEN=$(./wa-bridge api-token)
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8080/api/v1/messages?query=invoice&limit=5"
```

Windows PowerShell:

```powershell
$TOKEN = .\wa-bridge.exe api-token
curl.exe -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8080/api/v1/messages?query=invoice&limit=5"
```

You should see JSON with `"ok": true` and a `messages` list. The full list of endpoints is in the
[HTTP API reference](../reference/http-api.md), and the bridge serves its OpenAPI document at
`/api/v1/openapi.yaml`.

### 8. Send from Python

Sending needs `sending.enabled`, see [Scheduled and human-like sending](08-scheduled-and-human-like-sending.md).
The SDK has no approval step, so only your code decides.

```python
from wa_mcp import config
from wa_mcp.client import Bridge

with Bridge(cfg=config.load("config.json")) as bridge:
    result = bridge.send("15550100001", text="Hello from my script", human=True)
    print(result["id"], result["waited_ms"])
```

You should see the message id and the time spent on human-like waits. Replace the number with your own.

### 9. An auto-reply bot (optional, handle with care)

[`examples/auto_reply_bot.py`](../../examples/auto_reply_bot.py) answers allowed contacts by itself. It sends
real messages, so read the warning at the top of the file. It is off until you set
`WA_MCP_BOT_ENABLE=yes` and an allow-list in `WA_MCP_BOT_ALLOW`, it answers only direct chats from that
list, never in quiet hours, and only a few times per chat. Start with `--dry-run`:

```bash
export WA_MCP_BOT_ALLOW="15550100002"
export WA_MCP_BOT_ENABLE=yes
uv run python examples/auto_reply_bot.py --dry-run
```

You should see a line for each message that arrives, with the decision and the reason, and no message sent.

## Troubleshooting

- Nothing arrives. Check that the log of the bridge says `webhooks: 1 configured`, that the URL and port match
  the receiver, and that you restarted the bridge after editing `config.json`.
- The receiver answers 401 to real deliveries. The secret differs: the bridge must run in a shell that has the
  same `WA_MCP_WEBHOOK_SECRET`. Another cause is a clock that is more than 5 minutes off.
- An event arrives twice. A delivery that was not acknowledged within 15 seconds is retried with the same
  delivery id. The receiver in `examples/` skips repeats. Keep a list of seen ids in your own code too.
- `bridge not reachable`. Start the bridge, and check that both programs read the same `config.json`.
- `no API token`. Start the bridge once as the same user, or set `api.token` in the config.

## Next steps

- [Webhooks](../guides/webhooks.md): every event, the payloads and the verification code in Python and Node.js.
- [HTTP API](../reference/http-api.md): the endpoints with examples.
- [Embed the bridge](../guides/embedding.md): run the bridge inside your own Go program.
