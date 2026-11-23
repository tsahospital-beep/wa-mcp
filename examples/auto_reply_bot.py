"""An opt-in auto-reply bot built on WA MCP webhooks and the bridge send endpoint.

WARNING. This program sends WhatsApp messages by itself. Replies reach real people. Automated use of a
WhatsApp account can go against the WhatsApp terms of service, and WhatsApp can limit or block accounts
that send automated messages. Try it with a test account and a test contact first. The human-like
pacing of the bridge makes sending look less mechanical. It does not guarantee anything.

The bot is off. It does nothing until you set `WA_MCP_BOT_ENABLE=yes`, and it refuses to start without an
allow-list. It replies only when all of this holds:

* the webhook is signed correctly and new (same checks as examples/webhook_receiver.py);
* the message is a plain text message that someone else sent in a direct chat. Groups are never answered;
* the chat is in the allow-list `WA_MCP_BOT_ALLOW`, a comma separated list of chat addresses or phone
  numbers, for example `15550100002,15550100003@s.whatsapp.net`. A person can also appear under an address
  that ends in `@lid`. Run once with `--dry-run` and read the logged chat to see which form arrives;
* it is outside quiet hours (local time, `--quiet 22:00-08:00` by default);
* the bot has not answered this chat within `--cooldown-minutes` (60) and has answered it fewer than
  `--max-per-day` (3) times in the last 24 hours. This also stops two bots from answering each other.

The bridge adds its own checks: `sending.enabled` must be true, and the limits, duplicate protection and
quiet hours of the `sending` section apply. The reply goes through the normal send pipeline with
human-like waits. The webhook is acknowledged at once and the send runs in a worker thread, so a slow
send does not cause the bridge to retry the delivery.

Setup (the bridge needs `sending.enabled` and a `message` webhook that points here):

    export WA_MCP_WEBHOOK_SECRET="$(openssl rand -hex 32)"
    export WA_MCP_BOT_ALLOW="15550100002"
    export WA_MCP_BOT_ENABLE=yes
    uv run python examples/auto_reply_bot.py --config config.json --dry-run

Remove `--dry-run` when the logged decisions look right. Counters live in memory and start again when
the program restarts. Stop it with Ctrl+C.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as clock_time

from wa_mcp import config as wa_config
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.console import configure_console
from webhook_receiver import MAX_AGE_SECONDS, WEBHOOK_VAR, Delivery, build_server, mask_jid

ENABLE_VAR = "WA_MCP_BOT_ENABLE"
ALLOW_VAR = "WA_MCP_BOT_ALLOW"
DAY_SECONDS = 86_400
DIRECT_SERVERS = ("@s.whatsapp.net", "@lid")


@dataclass(frozen=True)
class Policy:
    allow: frozenset[str]
    quiet_start: clock_time
    quiet_end: clock_time
    cooldown_seconds: float
    max_per_day: int
    reply: str


@dataclass
class State:
    """When the bot answered each chat, newest last. Take `lock` around decide and reserve."""

    replies: dict[str, list[float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def recent(self, chat: str, now: float) -> list[float]:
        kept = [t for t in self.replies.get(chat, []) if now - t < DAY_SECONDS]
        self.replies[chat] = kept
        return kept


@dataclass(frozen=True)
class Decision:
    send: bool
    reason: str


def normalize(address: str) -> str:
    """A phone number or a chat address in the form the bridge reports (digits, then the server)."""
    text = address.strip()
    if "@" in text:
        user, _, server = text.partition("@")
        return f"{user.split(':', 1)[0].split('.', 1)[0]}@{server.lower()}"
    digits = "".join(c for c in text if c.isdigit())
    return f"{digits}@s.whatsapp.net" if digits else ""


def parse_allow(value: str) -> frozenset[str]:
    return frozenset(a for a in (normalize(p) for p in value.split(",")) if a)


def parse_clock(text: str) -> clock_time:
    return datetime.strptime(text.strip(), "%H:%M").time()


def parse_quiet(text: str) -> tuple[clock_time, clock_time]:
    start, sep, end = text.partition("-")
    if not sep:
        raise ValueError("quiet hours must look like 22:00-08:00")
    return parse_clock(start), parse_clock(end)


def in_quiet_hours(now: datetime, start: clock_time, end: clock_time) -> bool:
    """True inside the daily window. The window may cross midnight. Equal times mean no window."""
    t = now.time()
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def reserve(state: State, chat: str, now: float) -> None:
    """Count a reply as sent when it is queued, so two quick messages do not both get an answer."""
    state.replies.setdefault(chat, []).append(now)


def decide(event: str, data: Mapping[str, object], policy: Policy, state: State, now: datetime) -> Decision:
    """Whether to answer this webhook. Pure except for reading the counters in state."""
    if event != "message":
        return Decision(False, f"event {event or 'unknown'} is not a message")
    if data.get("outgoing"):
        return Decision(False, "own message")
    if data.get("kind") != "text":
        return Decision(False, "not a text message")
    chat = normalize(str(data.get("conv", "")))
    if not chat.endswith(DIRECT_SERVERS):
        return Decision(False, "not a direct chat")
    if chat not in policy.allow:
        return Decision(False, "chat is not in the allow-list")
    if not str(data.get("body", "")).strip():
        return Decision(False, "empty text")
    if in_quiet_hours(now, policy.quiet_start, policy.quiet_end):
        return Decision(False, "quiet hours")
    recent = state.recent(chat, now.timestamp())
    if len(recent) >= policy.max_per_day:
        return Decision(False, "daily limit for this chat reached")
    if recent and now.timestamp() - recent[-1] < policy.cooldown_seconds:
        return Decision(False, "answered this chat a moment ago")
    return Decision(True, "allowed")


def say(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


def worker(jobs: queue.Queue[tuple[str, str]], bridge: Bridge | None) -> None:
    """Send the queued replies one at a time. bridge is None in a dry run."""
    while True:
        chat, text = jobs.get()
        try:
            if bridge is None:
                say(f"dry run: would answer {mask_jid(chat)}")
            else:
                result = bridge.send(chat, text=text, human=True)
                say(
                    f"answered {mask_jid(chat)} id={result.get('id', '')} waited_ms={result.get('waited_ms', 0)}"
                )
        except BridgeError as exc:
            say(f"send to {mask_jid(chat)} failed: {exc}")
        finally:
            jobs.task_done()


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        description="Opt-in auto-reply bot for WA MCP (read the module docstring)"
    )
    parser.add_argument("--config", help="path of config.json (default: WA_MCP_CONFIG or ./config.json)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--path", default="/hook")
    parser.add_argument("--reply", default="Thanks, I got your message. I will answer soon.")
    parser.add_argument("--quiet", default="22:00-08:00", help="quiet hours as HH:MM-HH:MM, local time")
    parser.add_argument("--cooldown-minutes", type=float, default=60.0)
    parser.add_argument("--max-per-day", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="log decisions and send nothing")
    args = parser.parse_args(argv)

    if os.environ.get(ENABLE_VAR) != "yes":
        sys.stderr.write(
            f"The bot is off. Read the warning in this file, then set {ENABLE_VAR}=yes to turn it on.\n"
        )
        return 2
    secret = os.environ.get(WEBHOOK_VAR, "")
    if len(secret) < 16:
        sys.stderr.write(
            f"set {WEBHOOK_VAR} to a secret of at least 16 characters, the same one the bridge uses\n"
        )
        return 2
    allow = parse_allow(os.environ.get(ALLOW_VAR, ""))
    if not allow:
        sys.stderr.write(f"set {ALLOW_VAR} to the chats the bot may answer, separated by commas\n")
        return 2
    try:
        quiet_start, quiet_end = parse_quiet(args.quiet)
    except ValueError as exc:
        sys.stderr.write(f"--quiet: {exc}\n")
        return 2

    policy = Policy(allow, quiet_start, quiet_end, args.cooldown_minutes * 60, args.max_per_day, args.reply)
    state = State()
    bridge = None if args.dry_run else Bridge(cfg=wa_config.load(args.config))
    jobs: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=50)
    threading.Thread(target=worker, args=(jobs, bridge), daemon=True).start()

    def on_delivery(delivery: Delivery) -> None:
        data = delivery.payload.get("data")
        fields = data if isinstance(data, dict) else {}
        chat = normalize(str(fields.get("conv", "")))
        now = datetime.now()
        with state.lock:
            decision = decide(delivery.event, fields, policy, state, now)
            if decision.send:
                reserve(state, chat, now.timestamp())
        say(f"{delivery.event or 'unknown'} {mask_jid(chat) or '-'}: {decision.reason}")
        if not decision.send:
            return
        try:
            jobs.put_nowait((chat, policy.reply))
        except queue.Full:
            say("reply queue is full, skipped")

    server = build_server(args.host, args.port, secret.encode(), on_delivery, args.path, MAX_AGE_SECONDS)
    mode = "dry run" if args.dry_run else "live"
    say(
        f"bot on ({mode}): {len(allow)} allowed chat(s), quiet hours {args.quiet}, http://{args.host}:{server.server_port}{args.path}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if bridge is not None:
            bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
