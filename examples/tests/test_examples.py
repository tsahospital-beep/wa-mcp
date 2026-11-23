"""Tests for the example programs. They use only the loopback interface and fake data."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path

import auto_reply_bot as bot
import pytest
import python_sdk_basic

import webhook_receiver as hook
from wa_mcp import config

SECRET = b"0123456789abcdef0123456789abcdef"
EXAMPLES = Path(__file__).resolve().parent.parent


def signed(
    body: bytes, stamp: int | None = None, delivery: str = "d-1", secret: bytes = SECRET
) -> dict[str, str]:
    ts = str(int(time.time()) if stamp is None else stamp)
    return {
        "X-WA-MCP-Timestamp": ts,
        "X-WA-MCP-Delivery": delivery,
        "X-WA-MCP-Signature": hook.sign(secret, ts, delivery, body),
        "Content-Type": "application/json",
    }


def message_body(conv: str = "15550100002@s.whatsapp.net", text: str = "hello", **extra: object) -> bytes:
    data = {"conv": conv, "msg_id": "MSG1", "outgoing": False, "kind": "text", "body": text, **extra}
    return json.dumps({"event": "message", "at": 1, "data": data}).encode()


def post(url: str, body: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code


# webhook_receiver


def test_verify_accepts_a_signed_delivery_once() -> None:
    seen = hook.SeenDeliveries()
    body = message_body()
    now = time.time()
    first = hook.verify(SECRET, signed(body), body, now, seen)
    again = hook.verify(SECRET, signed(body), body, now, seen)
    assert first == hook.Verdict(True, 204, "accepted")
    assert again.accepted and again.duplicate


def test_verify_refuses_bad_signature_old_timestamp_and_missing_headers() -> None:
    seen = hook.SeenDeliveries()
    body = message_body()
    now = time.time()
    assert hook.verify(b"another secret value", signed(body), body, now, seen).status == 401
    assert hook.verify(SECRET, signed(body), body + b" ", now, seen).status == 401
    old = hook.verify(SECRET, signed(body, stamp=int(now) - 301), body, now, seen)
    assert old.status == 401 and "replay window" in old.reason
    assert hook.verify(SECRET, signed(body, stamp=int(now) + 301), body, now, seen).status == 401
    assert hook.verify(SECRET, {}, body, now, seen).status == 401
    assert hook.verify(SECRET, {**signed(body), "X-WA-MCP-Timestamp": "abc"}, body, now, seen).status == 401


def test_a_forged_delivery_does_not_use_up_the_delivery_id() -> None:
    seen = hook.SeenDeliveries()
    body = message_body()
    now = time.time()
    assert hook.verify(b"wrong secret value here", signed(body), body, now, seen).status == 401
    assert hook.verify(SECRET, signed(body), body, now, seen).duplicate is False


def test_the_signature_binds_the_delivery_id() -> None:
    seen = hook.SeenDeliveries()
    body = message_body()
    headers = signed(body, delivery="d-1")
    headers["X-WA-MCP-Delivery"] = "d-2"
    assert hook.verify(SECRET, headers, body, time.time(), seen).status == 401


def test_seen_deliveries_forget_old_ids_and_respect_the_limit() -> None:
    seen = hook.SeenDeliveries(window_seconds=10, limit=2)
    assert seen.add("a", 100.0) and not seen.add("a", 101.0)
    assert seen.add("b", 102.0) and seen.add("c", 103.0)  # "a" dropped by the limit
    assert seen.add("a", 104.0)
    assert seen.add("a", 200.0)  # outside the window


def test_header_names_are_case_insensitive() -> None:
    body = message_body()
    headers = {k.lower(): v for k, v in signed(body).items()}
    assert hook.verify(SECRET, headers, body, time.time(), hook.SeenDeliveries()).accepted


def test_server_delivers_each_accepted_event_once() -> None:
    got: list[hook.Delivery] = []
    server = hook.build_server("127.0.0.1", 0, SECRET, got.append)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/hook"
    try:
        body = message_body(text="secret words")
        assert post(url, body, signed(body)) == 204
        assert post(url, body, signed(body)) == 204  # a retry of the same delivery
        assert post(url, body, signed(body, delivery="d-2", secret=b"wrong secret value here")) == 401
        assert post(url + "x", body, signed(body, delivery="d-3")) == 404
    finally:
        server.shutdown()
        server.server_close()
    assert len(got) == 1
    assert got[0].event == "message" and got[0].delivery_id == "d-1"
    assert hook.message_id(got[0]) == "MSG1"


def test_summary_masks_the_address_and_hides_the_text() -> None:
    delivery = hook.Delivery("message", "d", 1, 1.0, json.loads(message_body(text="private words")))
    line = hook.summary(delivery, show_body=False)
    assert "1***0002@s.whatsapp.net" in line and "private" not in line and "id=MSG1" in line
    assert "private words" in hook.summary(delivery, show_body=True)


def test_jsonl_keeps_metadata_only(tmp_path: Path) -> None:
    delivery = hook.Delivery("message", "d-9", 123, 1000.5, json.loads(message_body(text="private words")))
    target = tmp_path / "log.jsonl"
    hook.append_jsonl(target, delivery)
    text = target.read_text(encoding="utf-8")
    assert json.loads(text) == {
        "delivery": "d-9",
        "event": "message",
        "msg_id": "MSG1",
        "timestamp": 123,
        "received_at": 1000,
    }
    assert "private" not in text


def test_receiver_needs_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hook.WEBHOOK_VAR, raising=False)
    assert hook.main(["--port", "0"]) == 2


# auto_reply_bot


def policy(**changes: object) -> bot.Policy:
    base = {
        "allow": frozenset({"15550100002@s.whatsapp.net"}),
        "quiet_start": clock_time(22, 0),
        "quiet_end": clock_time(8, 0),
        "cooldown_seconds": 3600.0,
        "max_per_day": 3,
        "reply": "Got it.",
    }
    base.update(changes)
    return bot.Policy(**base)  # type: ignore[arg-type]


NOON = datetime(2026, 11, 23, 12, 0)
MESSAGE = {"conv": "15550100002@s.whatsapp.net", "outgoing": False, "kind": "text", "body": "hi"}


def test_normalize_and_allow_list() -> None:
    assert bot.normalize("+1 555 010 0002") == "15550100002@s.whatsapp.net"
    assert bot.normalize("15550100002.0:12@s.whatsapp.net") == "15550100002@s.whatsapp.net"
    assert bot.normalize("99887766@LID") == "99887766@lid"
    assert bot.normalize("not a number") == ""
    assert bot.parse_allow("15550100002, 15550100003@s.whatsapp.net,,") == frozenset(
        {"15550100002@s.whatsapp.net", "15550100003@s.whatsapp.net"}
    )
    assert bot.parse_allow("") == frozenset()


def test_quiet_hours_cross_midnight() -> None:
    start, end = bot.parse_quiet("22:00-08:00")
    assert bot.in_quiet_hours(datetime(2026, 11, 23, 23, 30), start, end)
    assert bot.in_quiet_hours(datetime(2026, 11, 23, 7, 59), start, end)
    assert not bot.in_quiet_hours(datetime(2026, 11, 23, 8, 0), start, end)
    assert not bot.in_quiet_hours(NOON, start, end)
    day_start, day_end = bot.parse_quiet("13:00-14:00")
    assert bot.in_quiet_hours(datetime(2026, 11, 23, 13, 30), day_start, day_end)
    assert not bot.in_quiet_hours(NOON, clock_time(9, 0), clock_time(9, 0))
    with pytest.raises(ValueError, match="quiet hours"):
        bot.parse_quiet("22:00")


def test_decide_allows_only_what_the_policy_names() -> None:
    state = bot.State()
    assert bot.decide("message", MESSAGE, policy(), state, NOON) == bot.Decision(True, "allowed")
    cases = {
        "event": ("receipt", MESSAGE),
        "own": ("message", {**MESSAGE, "outgoing": True}),
        "kind": ("message", {**MESSAGE, "kind": "image"}),
        "group": ("message", {**MESSAGE, "conv": "120363000000000001@g.us"}),
        "stranger": ("message", {**MESSAGE, "conv": "15550100009@s.whatsapp.net"}),
        "empty": ("message", {**MESSAGE, "body": "  "}),
    }
    for name, (event, data) in cases.items():
        assert not bot.decide(event, data, policy(), state, NOON).send, name
    night = datetime(2026, 11, 23, 23, 0)
    assert bot.decide("message", MESSAGE, policy(), state, night).reason == "quiet hours"


def test_decide_applies_cooldown_and_daily_limit() -> None:
    state = bot.State()
    chat = "15550100002@s.whatsapp.net"
    now = NOON.timestamp()
    bot.reserve(state, chat, now - 600)
    assert bot.decide("message", MESSAGE, policy(), state, NOON).reason == "answered this chat a moment ago"
    state = bot.State()
    for hours_ago in (5, 4, 3):
        bot.reserve(state, chat, now - hours_ago * 3600)
    assert bot.decide("message", MESSAGE, policy(), state, NOON).reason == "daily limit for this chat reached"
    state = bot.State()
    bot.reserve(state, chat, now - 25 * 3600)
    assert bot.decide("message", MESSAGE, policy(), state, NOON).send  # older than a day: forgotten


def test_the_bot_is_off_without_the_enable_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (bot.ENABLE_VAR, bot.ALLOW_VAR, hook.WEBHOOK_VAR):
        monkeypatch.delenv(name, raising=False)
    assert bot.main(["--dry-run", "--port", "0"]) == 2
    monkeypatch.setenv(bot.ENABLE_VAR, "true")  # only the word "yes" turns it on
    assert bot.main(["--dry-run", "--port", "0"]) == 2
    monkeypatch.setenv(bot.ENABLE_VAR, "yes")
    monkeypatch.setenv(hook.WEBHOOK_VAR, SECRET.decode())
    assert bot.main(["--dry-run", "--port", "0"]) == 2  # no allow-list
    monkeypatch.setenv(bot.ALLOW_VAR, "15550100002")
    assert bot.main(["--dry-run", "--port", "0", "--quiet", "late"]) == 2


def read_lines(proc: subprocess.Popen[str]) -> queue.Queue[str]:
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()
    return lines


def wait_for(lines: queue.Queue[str], needle: str, seconds: float = 20.0) -> str:
    deadline = time.monotonic() + seconds
    seen: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        seen.append(line)
        if needle in line:
            return line
    raise AssertionError(f"no line with {needle!r}; saw {seen}")


def test_bot_dry_run_through_the_real_program() -> None:
    env = {
        **os.environ,
        bot.ENABLE_VAR: "yes",
        bot.ALLOW_VAR: "15550100002",
        hook.WEBHOOK_VAR: SECRET.decode(),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    cmd = [
        sys.executable,
        str(EXAMPLES / "auto_reply_bot.py"),
        "--dry-run",
        "--port",
        "0",
        "--quiet",
        "00:00-00:00",
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        lines = read_lines(proc)
        start = wait_for(lines, "bot on (dry run)")
        port = int(start.rsplit(":", 1)[1].split("/", 1)[0])
        url = f"http://127.0.0.1:{port}/hook"
        body = message_body()
        assert post(url, body, signed(body)) == 204
        wait_for(lines, "1***0002@s.whatsapp.net: allowed")
        wait_for(lines, "dry run: would answer")
        stranger = message_body(conv="15550100009@s.whatsapp.net")
        assert post(url, stranger, signed(stranger, delivery="d-2")) == 204
        wait_for(lines, "chat is not in the allow-list")
        assert post(url, body, signed(body, delivery="d-3", secret=b"wrong secret value here")) == 401
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# python_sdk_basic


def test_sdk_example_helpers() -> None:
    assert python_sdk_basic.short("a  b\nc", 10) == "a b c"
    assert python_sdk_basic.short("x" * 20, 10) == "xxxxxxx..."
    messages = [
        {"msg_id": "1", "kind": "text"},
        {"msg_id": "2", "kind": "image"},
        {"msg_id": "3", "kind": "voice"},
    ]
    assert python_sdk_basic.latest_attachment(messages) == messages[1]
    assert python_sdk_basic.latest_attachment(messages[:1]) is None
    assert python_sdk_basic.when(None) == "-"


def test_sdk_example_reports_an_unreachable_bridge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"api": {"listen": "127.0.0.1:1", "token": "t" * 40}}), encoding="utf-8")
    assert config.load(cfg_file).api.listen == "127.0.0.1:1"
    assert python_sdk_basic.main(["--config", str(cfg_file)]) == 1
    assert "not reachable" in capsys.readouterr().err
