"""Receive WA MCP webhooks and check them before you trust them.

A delivery is accepted only when all of this holds:

* the signature is `sha256=<hex>`, the HMAC-SHA256 of `<timestamp>.<delivery id>.<raw body>` with your
  secret, compared in constant time;
* the timestamp is within the replay window (5 minutes by default) of this machine's clock;
* the delivery id has not been processed before. A repeat is acknowledged with 204 and skipped, because
  the bridge retries a delivery with the same id when it does not get a 2xx answer.

Run it with the same secret that the bridge reads from `secret_env`:

    export WA_MCP_WEBHOOK_SECRET="$(openssl rand -hex 32)"
    uv run python examples/webhook_receiver.py --port 9000

In PowerShell set the secret with `$env:WA_MCP_WEBHOOK_SECRET = "..."` first. Then add the address to
`config.json` and restart the bridge:

    "webhooks": [{"url": "http://127.0.0.1:9000/hook", "events": ["message"], "secret_env": "WA_MCP_WEBHOOK_SECRET"}]

With `--port 0` the system picks a free port and the first output line shows it. The program prints one
line per accepted delivery and never prints message text unless you pass `--show-body`. `--jsonl FILE`
appends the metadata of each accepted delivery (delivery id, event, message id, timestamp) to a file.

The bridge delivers over plain HTTP. Keep the receiver on 127.0.0.1 or put it behind a TLS proxy.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

WEBHOOK_VAR = "WA_MCP_WEBHOOK_SECRET"
MAX_AGE_SECONDS = 300
MAX_BODY_BYTES = 1 << 20
SEEN_LIMIT = 10_000
JID_USER = re.compile(r"^(\d{7,})")


@dataclass(frozen=True)
class Verdict:
    """The result of checking one delivery."""

    accepted: bool
    status: int  # the HTTP status to answer with
    reason: str
    duplicate: bool = False


@dataclass(frozen=True)
class Delivery:
    """A delivery that passed every check."""

    event: str
    delivery_id: str
    timestamp: int
    received_at: float
    payload: Mapping[str, Any]


class SeenDeliveries:
    """Delivery ids seen inside the replay window. The oldest are dropped first."""

    def __init__(self, window_seconds: float = MAX_AGE_SECONDS, limit: int = SEEN_LIMIT) -> None:
        self._window = window_seconds
        self._limit = limit
        self._items: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, delivery_id: str, now: float) -> bool:
        """Remember the id. Returns False when it was already seen."""
        with self._lock:
            while self._items and (
                len(self._items) >= self._limit or now - next(iter(self._items.values())) > self._window
            ):
                self._items.popitem(last=False)
            if delivery_id in self._items:
                return False
            self._items[delivery_id] = now
            return True


def sign(secret: bytes, timestamp: str, delivery_id: str, body: bytes) -> str:
    """The signature header value the bridge sends: sha256=<hex of the HMAC>."""
    signed = f"{timestamp}.{delivery_id}.".encode() + body
    return "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()


def verify(
    secret: bytes,
    headers: Mapping[str, str],
    body: bytes,
    now: float,
    seen: SeenDeliveries,
    max_age: float = MAX_AGE_SECONDS,
) -> Verdict:
    """Check the headers of one delivery. Nothing is added to `seen` unless the signature is valid."""
    h = {k.lower(): v for k, v in headers.items()}
    stamp = h.get("x-wa-mcp-timestamp", "")
    delivery_id = h.get("x-wa-mcp-delivery", "")
    given = h.get("x-wa-mcp-signature", "")
    if not (stamp.isdigit() and delivery_id and given):
        return Verdict(False, 401, "missing or malformed headers")
    if abs(now - int(stamp)) > max_age:
        return Verdict(False, 401, "timestamp outside the replay window")
    if not hmac.compare_digest(sign(secret, stamp, delivery_id, body), given):
        return Verdict(False, 401, "signature does not match")
    if not seen.add(delivery_id, now):
        return Verdict(True, 204, "delivery id seen before", duplicate=True)
    return Verdict(True, 204, "accepted")


EventHandler = Callable[[Delivery], None]


def build_server(
    host: str,
    port: int,
    secret: bytes,
    on_delivery: EventHandler,
    path: str = "/hook",
    max_age: float = MAX_AGE_SECONDS,
) -> ThreadingHTTPServer:
    """An HTTP server that calls on_delivery once for every accepted, new delivery."""
    seen = SeenDeliveries(window_seconds=max_age)

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != path:
                self._answer(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0:
                self._answer(400)
                return
            if length > MAX_BODY_BYTES:
                self._answer(413)
                return
            body = self.rfile.read(length)
            now = time.time()
            verdict = verify(secret, dict(self.headers.items()), body, now, seen, max_age)
            if verdict.accepted and not verdict.duplicate:
                try:
                    payload = json.loads(body)
                except ValueError:
                    self._answer(400)
                    return
                if not isinstance(payload, dict):
                    self._answer(400)
                    return
                on_delivery(
                    Delivery(
                        event=str(payload.get("event", "")),
                        delivery_id=self.headers.get("X-WA-MCP-Delivery", ""),
                        timestamp=int(self.headers.get("X-WA-MCP-Timestamp", "0")),
                        received_at=now,
                        payload=payload,
                    )
                )
            self._answer(verdict.status)

        def _answer(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return  # the default log line would carry the request path

    return ThreadingHTTPServer((host, port), Hook)


def mask_jid(jid: str) -> str:
    """15550100002@s.whatsapp.net becomes 1***0002@s.whatsapp.net."""
    user, at, server = jid.partition("@")
    if not JID_USER.match(user) or len(user) < 6:
        return jid
    return f"{user[0]}***{user[-4:]}{at}{server}"


def summary(delivery: Delivery, show_body: bool) -> str:
    data = delivery.payload.get("data")
    fields = data if isinstance(data, dict) else {}
    parts = [delivery.event or "unknown"]
    for key in ("conv", "chat", "peer"):
        if isinstance(fields.get(key), str):
            parts.append(f"{key}={mask_jid(fields[key])}")
            break
    for key in ("kind", "type", "state", "status", "outcome"):
        if fields.get(key):
            parts.append(f"{key}={fields[key]}")
    msg_id = message_id(delivery)
    if msg_id:
        parts.append(f"id={msg_id}")
    if show_body and isinstance(fields.get("body"), str):
        parts.append(f"body={fields['body'][:80]!r}")
    return " ".join(parts)


def message_id(delivery: Delivery) -> str:
    data = delivery.payload.get("data")
    if not isinstance(data, dict):
        return ""
    value = data.get("msg_id") or data.get("message_id") or data.get("id") or ""
    return str(value)


def append_jsonl(path: Path, delivery: Delivery) -> None:
    """Append the metadata of one delivery. The file is created for your user only."""
    record = {
        "delivery": delivery.delivery_id,
        "event": delivery.event,
        "msg_id": message_id(delivery),
        "timestamp": delivery.timestamp,
        "received_at": int(delivery.received_at),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and print WA MCP webhooks")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9000, help="port, 0 picks a free one (default 9000)")
    parser.add_argument("--path", default="/hook", help="URL path (default /hook)")
    parser.add_argument("--max-age", type=float, default=MAX_AGE_SECONDS, help="replay window in seconds")
    parser.add_argument(
        "--show-body", action="store_true", help="print the first 80 characters of message text"
    )
    parser.add_argument(
        "--jsonl", type=Path, help="append the metadata of each accepted delivery to this file"
    )
    args = parser.parse_args(argv)

    secret = os.environ.get(WEBHOOK_VAR, "")
    if len(secret) < 16:
        sys.stderr.write(
            f"set {WEBHOOK_VAR} to a secret of at least 16 characters, the same one the bridge uses\n"
        )
        return 2

    def on_delivery(delivery: Delivery) -> None:
        print(f"{time.strftime('%H:%M:%S')} {summary(delivery, args.show_body)}", flush=True)
        if args.jsonl:
            append_jsonl(args.jsonl, delivery)

    server = build_server(args.host, args.port, secret.encode(), on_delivery, args.path, args.max_age)
    print(f"listening on http://{args.host}:{server.server_port}{args.path} (pid {os.getpid()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
