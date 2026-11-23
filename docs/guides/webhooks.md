# Webhooks

The bridge can post events to your own HTTP endpoints, for example to trigger a script when a message
arrives. Each request carries a timestamp and an HMAC-SHA256 signature so that your endpoint can check
where it came from and that it is not a replay of an old delivery.

## Configure

Add entries to `webhooks` in `config.json`:

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

| Key | Meaning |
| --- | --- |
| `url` | `http` or `https` address. |
| `events` | Which events to send. An empty or missing list means all of them. |
| `secret_env` | Name of the environment variable that holds the secret. The variable must be set for the bridge process. |

An unknown event name or a URL that is not `http` or `https` stops the bridge at start. When no secret is
set, the bridge still sends the events, without a signature, and writes a warning to its log.

Generate a long random secret and set it before you start the bridge:

```bash
export WA_MCP_WEBHOOK_SECRET="$(openssl rand -hex 32)"
./wa-bridge
```

In PowerShell use `$env:WA_MCP_WEBHOOK_SECRET = "..."`.

## Events

| Event | When | `data` |
| --- | --- | --- |
| `message` | A message arrives live, or you send one from any device. Messages that come in through history sync are not sent. | The stored message: `conv`, `msg_id`, `author`, `outgoing`, `sent_at`, `body`, `kind` and, when present, `attachment_name`, `mime`, `byte_size`, `reply_to_id`, `reply_to_author`, `reply_preview`, `mentioned`. |
| `receipt` | Delivery, read or played receipts. | `chat`, `ids`, `type` (`delivered`, `read`, `played` and similar), `from`, `from_me`, `at`. |
| `call` | A call starts, is answered, rejected or ends. | The call record: `call_id`, `peer`, `outgoing`, `video`, `started_at`, `outcome` and, when known, `group_jid` and `ended_at`. |
| `connection` | The state of the WhatsApp connection changes. | `state`: `connected`, `disconnected`, `replaced`, `temporary_ban` (with `detail`) or `logged_out`. |
| `scheduled` | A scheduled message was sent or failed. | `id`, `status`, `target` and either `message_id` (sent) or `error` and `attempts` (failed), plus `next_run`. |
| `agent.reply` | The connector drafts, skips, queues or sends a reply. | `status`, `reason`, `chat`, `message_id`, `draft_id`. See [Agent connector](agent-connector.md). |
| `security` | A session event: a logout, another client with this session's keys, a device added to or removed from the account, another account at login, a new link. | `at`, `kind` (`logged_out`, `session_replaced`, `device_added`, `device_removed`, `account_mismatch`, `paired`), `level` (`warning` or `info`) and `detail`. Numbers are masked. |

`kind` is `text`, `image`, `video`, `audio`, `voice`, `document`, `sticker`, `location`, `contact` or
`poll`. The last three have no attachment and carry a text summary in `body`.

Edits, deletions and reactions update the database but are not sent as events. Timestamps are unix
seconds. Attachments are not sent in the payload. Download them with the `media/download` endpoint or
the `download_media` tool.

## Request format

Every call is a `POST` with a JSON body:

```json
{
  "event": "message",
  "at": 1795426200,
  "account": "default",
  "self": "+1 *** *** 0101",
  "data": {
    "conv": "15550100002@s.whatsapp.net",
    "msg_id": "3EB0A1B2C3D4E5F6",
    "author": "15550100002@s.whatsapp.net",
    "outgoing": false,
    "sent_at": 1795426198,
    "body": "Can you send the new invoice today?",
    "kind": "text"
  }
}
```

`account` names the account in `accounts` (`default` without accounts) and `self` is your own number,
masked like in the status, so one receiver can serve several numbers. An account can have its own list
in `accounts.<name>.webhooks`.

Headers:

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `User-Agent` | `wa-mcp-bridge/<version>` |
| `X-WA-MCP-Event` | The event name. |
| `X-WA-MCP-Delivery` | A UUID. Retries of one delivery use the same value, so you can drop repeats. |
| `X-WA-MCP-Timestamp` | Unix seconds when this attempt was made. A retry carries a new timestamp. |
| `X-WA-MCP-Signature` | `sha256=<hex>`, the HMAC-SHA256 with your secret over the timestamp, a dot, the delivery id, a dot and the raw request body. Present only when a secret is set. |

The signed text is `<timestamp>.<delivery id>.<raw body>`. Binding the timestamp and the delivery id to
the signature lets you refuse deliveries that someone recorded and sends again. Reject a request when the
timestamp is more than 5 minutes away from your own clock, when the signature does not match, or when you
have already processed that delivery id.

Answer with any 2xx status to acknowledge. Anything else, no answer within 15 seconds, or a redirect
(the bridge does not follow redirects) counts as a failure and is retried after 1, 5 and 25 seconds. After
the fourth failed attempt the bridge logs the failure and drops the event.

## Delivery limits

- Every URL has its own queue of 500 events and its own worker. A slow or failing endpoint does not hold
  up the others. When a queue is full, new events for that URL are dropped and the bridge logs how many.
- Events are not stored. If the bridge stops, events that have not been delivered are lost. Use the
  database or `list_messages` to catch up.
- Delivery order is not guaranteed, because retries are sent later than newer events.

## Verify the signature

Read the raw bytes of the body, before any JSON parsing. Compute the HMAC over
`<timestamp>.<delivery id>.<body>`, compare in constant time, check the age of the timestamp and keep
the delivery ids you have seen in the last few minutes.

### Python

```python
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["WA_MCP_WEBHOOK_SECRET"].encode()
MAX_AGE_SECONDS = 300
SEEN = {}  # delivery id -> time first seen


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        stamp = self.headers.get("X-WA-MCP-Timestamp", "")
        delivery = self.headers.get("X-WA-MCP-Delivery", "")
        given = self.headers.get("X-WA-MCP-Signature", "")
        signed = f"{stamp}.{delivery}.".encode() + body
        expected = "sha256=" + hmac.new(SECRET, signed, hashlib.sha256).hexdigest()
        now = time.time()
        fresh = stamp.isdigit() and abs(now - int(stamp)) <= MAX_AGE_SECONDS
        if not (fresh and hmac.compare_digest(expected, given)):
            self.send_response(401)
            self.end_headers()
            return
        for old in [d for d, t in SEEN.items() if now - t > MAX_AGE_SECONDS]:
            del SEEN[old]
        if delivery in SEEN:  # a repeat: acknowledge, do not process again
            self.send_response(204)
            self.end_headers()
            return
        SEEN[delivery] = now
        event = json.loads(body)
        print(event["event"], event["data"])
        self.send_response(204)
        self.end_headers()


HTTPServer(("127.0.0.1", 9000), Hook).serve_forever()
```

### Node.js

```javascript
import crypto from "node:crypto";
import http from "node:http";

const secret = process.env.WA_MCP_WEBHOOK_SECRET;
const maxAgeSeconds = 300;
const seen = new Map(); // delivery id -> time first seen

http
  .createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      const stamp = String(req.headers["x-wa-mcp-timestamp"] ?? "");
      const delivery = String(req.headers["x-wa-mcp-delivery"] ?? "");
      const given = String(req.headers["x-wa-mcp-signature"] ?? "");
      const expected =
        "sha256=" +
        crypto
          .createHmac("sha256", secret)
          .update(Buffer.concat([Buffer.from(stamp + "." + delivery + "."), body]))
          .digest("hex");
      const fresh =
        /^\d+$/.test(stamp) && Math.abs(Date.now() / 1000 - Number(stamp)) <= maxAgeSeconds;
      const valid =
        fresh &&
        given.length === expected.length &&
        crypto.timingSafeEqual(Buffer.from(given), Buffer.from(expected));
      if (!valid) {
        res.writeHead(401).end();
        return;
      }
      const now = Date.now() / 1000;
      for (const [id, t] of seen) if (now - t > maxAgeSeconds) seen.delete(id);
      if (seen.has(delivery)) {
        res.writeHead(204).end(); // a repeat: acknowledge, do not process again
        return;
      }
      seen.set(delivery, now);
      const event = JSON.parse(body.toString("utf8"));
      console.log(event.event, event.data);
      res.writeHead(204).end();
    });
  })
  .listen(9000, "127.0.0.1");
```

## Test an endpoint

You can check a receiver without WhatsApp by signing a body yourself:

```bash
BODY='{"event":"message","at":1795426200,"data":{"conv":"15550100002@s.whatsapp.net","msg_id":"TEST1","author":"15550100002@s.whatsapp.net","outgoing":false,"sent_at":1795426198,"body":"test","kind":"text"}}'
STAMP="$(date +%s)"
DELIVERY="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
SIG="sha256=$(printf '%s.%s.%s' "$STAMP" "$DELIVERY" "$BODY" | openssl dgst -sha256 -hmac "$WA_MCP_WEBHOOK_SECRET" | sed 's/^.* //')"
curl -i -X POST http://127.0.0.1:9000/hook \
  -H "Content-Type: application/json" -H "X-WA-MCP-Delivery: $DELIVERY" \
  -H "X-WA-MCP-Timestamp: $STAMP" -H "X-WA-MCP-Signature: $SIG" -d "$BODY"
```

## Privacy

A webhook sends message text to the URL you configure. Keep the receiver on your own machine or network,
use `https` for anything that leaves it, and keep the secret out of version control.
