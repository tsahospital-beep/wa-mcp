"""End-to-end check of WA MCP with a real test WhatsApp account. Run it by hand, never in CI.

    uv run python scripts/e2e_check.py --dry-run
    uv run python scripts/e2e_check.py --self-number 15550100001 --group "Test group"

The script runs against a bridge that is already running and linked, and talks to it the way users and
assistants do: the HTTP API, the MCP server over stdio and over HTTP, and a webhook receiver. Every step
ends as PASS, FAIL or SKIPPED, with evidence, and the results go into a Markdown report.

What it can send, and where. It sends only to the number you give with --self-number (your own
"message yourself" chat) and, when you name one with --group, to that test group. Nothing goes to any other
chat. Before anything that other people can see (a group message, a read receipt) it prints the exact text
and the recipient and waits until you type "send". Before it sends to your own number it asks once. A test
account is strongly recommended. Automated use of a WhatsApp account may go against the WhatsApp terms of
service.

The script never reads the store while it is in use for anything except one read-only snapshot in the
encryption step, and it deletes that copy. The report masks phone numbers and tokens. Screenshots you drop
into the run folder are only listed (name, size, checksum), never read.

`--dry-run` prints the plan and touches nothing: no connection, no file, no process. The guide is
docs/testing/e2e.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mmap
import os
import platform
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from wa_mcp import __version__ as sdk_version
from wa_mcp import config as wa_config
from wa_mcp import crypt
from wa_mcp import db as wa_db
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.console import configure_console

ROOT = Path(__file__).resolve().parent.parent
RECEIVER = ROOT / "examples" / "webhook_receiver.py"
PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
# A Greek word (kalimera) built from code points, so that this file stays plain ASCII and the search step has a non-Latin query.
NON_LATIN_WORD = "".join(chr(c) for c in (0x3BA, 0x3B1, 0x3BB, 0x3B7, 0x3BC, 0x3AD, 0x3C1, 0x3B1))
TEST_CHAT_NUMBER = (
    "15550100999"  # a fictional number: the import creates a local chat for it, nothing is sent
)
EXPORT_NAME = "WhatsApp Chat with E2E Friend.txt"
WEBHOOK_VAR = "WA_MCP_WEBHOOK_SECRET"
MEDIA_MAGIC = {
    "image": (b"\xff\xd8\xff", b"\x89PNG", b"RIFF"),
    "voice": (b"OggS",),
    "audio": (b"OggS", b"ID3", b"\xff\xfb", b"RIFF", b"\x00\x00\x00"),
}


# Results and the report


@dataclass
class StepResult:
    id: str
    title: str
    status: str
    seconds: float
    evidence: list[str] = field(default_factory=list)
    note: str = ""
    started: str = ""


_JID = re.compile(r"(?<!\d)(\d{7,20})((?::\d+)?(?:\.\d+)?@(?:s\.whatsapp\.net|lid|g\.us))")
_FOLDER = re.compile(r"(?<!\d)(\d{7,20})(_(?:s\.whatsapp\.net|lid|g\.us))")
_TOKEN = re.compile(r"\bwam_[A-Za-z0-9_-]{6,}")
_HEX_SECRET = re.compile(r"\b[0-9a-fA-F]{48,}\b")


def redact(text: str) -> str:
    """Mask phone numbers in addresses and folder names and hide tokens and long hex secrets."""

    def mask(m: re.Match[str]) -> str:
        return f"{m.group(1)[0]}***{m.group(1)[-4:]}{m.group(2)}"

    text = text.replace("\u2022\u2022\u2022", "***")
    text = _TOKEN.sub("wam_[hidden]", text)
    text = _HEX_SECRET.sub("[hidden]", text)
    return _FOLDER.sub(mask, _JID.sub(mask, text))


def mask_number(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"{digits[0]}***{digits[-4:]}" if len(digits) >= 7 else "***"


def matches_masked(number: str, masked: str) -> bool:
    """Whether a typed number fits the masked number of the status endpoint (first digit and last four)."""
    mine = "".join(c for c in number if c.isdigit())
    shown = "".join(c for c in masked if c.isdigit())
    if len(mine) < 7 or len(shown) < 5:
        return False
    return mine[0] == shown[0] and mine[-4:] == shown[-4:]


def table_cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def code(text: str) -> str:
    return f"`{text}`" if "`" not in text else text


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int
    sha256: str
    modified: str


def list_files(folder: Path) -> list[FileInfo]:
    """Name, size and checksum of the files in a folder. The files are read only to compute the checksum."""
    if not folder.is_dir():
        return []
    out: list[FileInfo] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        out.append(FileInfo(path.name, path.stat().st_size, digest.hexdigest()[:16], stamp))
    return out


class Report:
    """Collects step results and writes them as Markdown."""

    def __init__(self, run_id: str, started: datetime, facts: dict[str, str]) -> None:
        self.run_id = run_id
        self.started = started
        self.facts = facts
        self.results: list[StepResult] = []
        self.finished: datetime | None = None

    def add(self, result: StepResult) -> None:
        self.results.append(result)

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for r in self.results if r.status == s) for s in (PASS, FAIL, SKIPPED)}

    def verdict(self) -> str:
        c = self.counts()
        if c[FAIL]:
            return "FAILED"
        if not c[PASS]:
            return "NOTHING WAS CHECKED"
        return "PASSED with skipped steps" if c[SKIPPED] else "PASSED"

    def render(self, files: list[FileInfo] | None = None) -> str:
        c = self.counts()
        lines = [
            f"# WA MCP end-to-end report {self.run_id}",
            "",
            f"Result: **{self.verdict()}** ({c[PASS]} passed, {c[FAIL]} failed, {c[SKIPPED]} skipped)",
            "",
            f"- Started: {self.started.isoformat(timespec='seconds')}",
            f"- Finished: {self.finished.isoformat(timespec='seconds') if self.finished else 'not finished'}",
        ]
        lines += [f"- {key}: {value}" for key, value in self.facts.items()]
        lines += [
            "",
            "Phone numbers and tokens are masked in this report. Message text is not included except for the",
            "test messages that this script wrote itself.",
            "",
            "## Summary",
            "",
            "| Step | Result | Seconds | Note |",
            "| --- | --- | --- | --- |",
        ]
        for r in self.results:
            lines.append(f"| {r.id} | {r.status} | {r.seconds:.1f} | {table_cell(redact(r.note))} |")
        for r in self.results:
            lines += [
                "",
                f"## {r.id}: {r.title}",
                "",
                f"Result: **{r.status}**. Started {r.started or 'n/a'}.",
            ]
            if r.note:
                lines += ["", redact(r.note)]
            if r.evidence:
                lines.append("")
                lines += [f"- {redact(item)}" for item in r.evidence]
        lines += ["", "## Files in the run folder", ""]
        if files:
            lines += ["| File | Size | SHA-256 (first 16) | Modified |", "| --- | --- | --- | --- |"]
            lines += [f"| {code(f.name)} | {f.size} | {f.sha256} | {f.modified} |" for f in files]
        else:
            lines.append("No screenshots or other files were dropped into the run folder.")
        return "\n".join(lines) + "\n"

    def write(self, path: Path, files: list[FileInfo] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(files), encoding="utf-8")
        return path


# Pure helpers


def expected_wait_window(s: wa_config.SendingConfig, text_len: int) -> tuple[float, float]:
    """The shortest and longest human-like wait in seconds for one text of this length.

    Reading pause 0.4 to 1.5 s, then typing time (length over typing speed, kept between the minimum and the
    maximum, varied by plus or minus jitter), plus the longest pause between messages to one chat."""
    typing = min(max(text_len / s.typing_chars_per_second, s.typing_min_seconds), s.typing_max_seconds)
    low = 0.4 + typing * (1 - s.jitter)
    high = 1.5 + typing * (1 + s.jitter) + s.pause_between_messages_seconds[1]
    return low - 0.5, high + 2.0


def word_overlap(expected: str, text: str) -> float:
    """Share of the expected words that appear in the text, ignoring case and punctuation."""
    words = [w for w in re.findall(r"\w+", expected.casefold()) if w]
    if not words:
        return 0.0
    found = set(re.findall(r"\w+", text.casefold()))
    return sum(1 for w in words if w in found) / len(words)


def make_test_export(folder: Path, marker: str) -> tuple[Path, int]:
    """Write a small Android-style export. Returns the file and the number of messages in it."""
    lines = [
        f"13/01/2024, 09:00 - E2E Me: Export test {marker}",
        "13/01/2024, 09:01 - E2E Friend: Good morning",
        f"13/01/2024, 09:02 - E2E Friend: {NON_LATIN_WORD} from the export",
        "13/01/2024, 09:05 - E2E Me: A message with",
        "a second line",
        "14/01/2024, 18:30 - E2E Friend: <Media omitted>",
        "14/01/2024, 18:31 - E2E Me: See you tomorrow",
    ]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / EXPORT_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, 6


def sniff_ok(kind: str, head: bytes) -> bool:
    magics = MEDIA_MAGIC.get(kind)
    return True if magics is None else any(head.startswith(m) for m in magics)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def parse_sse_or_json(resp: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of an MCP over HTTP answer, which may be plain JSON or one server-sent event."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                return data if isinstance(data, dict) else {}
        return {}
    data = resp.json()
    return data if isinstance(data, dict) else {}


# Run context


class Skip(Exception):
    """The step cannot run here. The text says why."""


class Fail(Exception):
    """The step ran and the result is wrong. The text says what."""


class NoInput(Skip):
    """A step needs a person at the keyboard and none is there."""


class Interrupted(Exception):
    """The person pressed Ctrl+C."""


@dataclass
class Ctx:
    args: argparse.Namespace
    cfg: wa_config.Config
    run_id: str
    run_dir: Path
    bridge: Bridge
    state: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    mcp: McpStdio | None = None
    started_at: float = field(default_factory=time.time)

    # output and input

    def say(self, text: str = "") -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def note(self, key: str, value: object) -> None:
        self.evidence.append(f"{key}: {value}")

    def _read(self, prompt: str) -> str:
        if not sys.stdin.isatty():
            raise NoInput("this step needs a person at the keyboard (stdin is not a terminal)")
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            return input().strip()
        except EOFError as exc:
            raise NoInput("no input available") from exc
        except KeyboardInterrupt as exc:
            raise Interrupted from exc

    def ask(self, question: str, default: str = "") -> str:
        answer = self._read(f"{question} ")
        return answer or default

    def yes_no_skip(self, question: str) -> str:
        """y, n or s (skip)."""
        while True:
            answer = self._read(f"{question} [y/n/s=skip] ").lower()
            if answer in ("y", "n", "s"):
                return answer

    def pause(self, text: str) -> None:
        self._read(f"{text} Press Enter to continue. ")

    def confirm_visible(self, what: str, where: str) -> None:
        """Other people can see the result. Nothing happens until the word send is typed."""
        self.say("")
        self.say("VISIBLE TO OTHER PEOPLE")
        self.say(f"  Recipient: {where}")
        self.say(f"  Action:    {what}")
        if self._read("Type send to go on, anything else skips this step: ").lower() != "send":
            raise Skip("not confirmed by the person running the check")

    def confirm_self(self, what: str) -> None:
        """Messages to your own number. They are not visible to other contacts."""
        self.say("")
        self.say("This step sends to your own number only (the message yourself chat).")
        self.say(f"  Action: {what}")
        if self._read("Type yes to go on, anything else skips this step: ").lower() not in ("yes", "y"):
            raise Skip("not confirmed by the person running the check")

    def confirm_local(self, what: str) -> None:
        """Changes only local state. Nothing is sent to WhatsApp."""
        self.say("")
        self.say("This step does not send anything to WhatsApp.")
        self.say(f"  Action: {what}")
        if self._read("Type yes to go on, anything else skips this step: ").lower() not in ("yes", "y"):
            raise Skip("not confirmed by the person running the check")

    # state shared between steps

    def own(self) -> str:
        number = str(self.state.get("own", "")) or "".join(
            c for c in str(self.args.self_number or "") if c.isdigit()
        )
        if not number:
            raise Skip("needs the number of the test account: run the preflight step or pass --self-number")
        return number

    def sending_on(self) -> None:
        if "sending_enabled" not in self.state:
            self.state["sending_enabled"] = bool(self.bridge.status().get("sending_enabled"))
        if not self.state["sending_enabled"]:
            raise Skip("sending is off: set sending.enabled to true in config.json and restart the bridge")

    def marker(self) -> str:
        return f"wa-mcp-e2e-{self.run_id}"

    def mcp_call(self, tool: str, arguments: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
        if self.mcp is None:
            raise Skip("the MCP server could not be started")
        result = self.mcp.call(tool, arguments, timeout)
        if result.get("ok") is False:
            raise Fail(f"{tool} answered: {result.get('error', 'an error')}")
        return result

    def group(self) -> str:
        """The JID of the test group from --group (a JID or an exact title)."""
        if "group" in self.state:
            return str(self.state["group"])
        wanted = str(self.args.group or "")
        if not wanted:
            raise Skip("no test group named: pass --group with the JID or the exact title of a test group")
        if wanted.endswith("@g.us"):
            self.state["group"] = wanted
            return wanted
        hits = [
            c
            for c in self.bridge.chats(query=wanted, limit=50)
            if c.get("is_group") and str(c.get("title", "")).casefold() == wanted.casefold()
        ]
        if len(hits) != 1:
            raise Skip(f"--group {wanted!r} matches {len(hits)} groups; pass the JID of one test group")
        self.state["group"] = str(hits[0]["jid"])
        return str(self.state["group"])

    def wait_for(self, check: Callable[[], Any], seconds: float, label: str, every: float = 3.0) -> Any:
        """Call check until it returns something other than None or False. Returns it, or None on timeout."""
        deadline = time.monotonic() + seconds
        try:
            while True:
                value = check()
                if value:
                    return value
                if time.monotonic() >= deadline:
                    self.say(f"  gave up waiting for {label}")
                    return None
                time.sleep(every)
        except KeyboardInterrupt:
            self.say(f"  stopped waiting for {label}")
            return None


# MCP over stdio, driven from synchronous code


class McpStdio:
    """One session with the real wa-mcp server over stdio."""

    def __init__(self, config_file: Path) -> None:
        self.config_file = config_file
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._session: Any = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "wa_mcp_server", "--config", str(self.config_file), "--toolsets", "all"],
            env=dict(os.environ),
        )
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            self._error = exc
            raise
        finally:
            self._ready.set()

    def start(self, timeout: float = 90.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout) or self._session is None:
            raise Skip(f"the MCP server did not start: {self._error or 'timed out'}")

    def call(self, tool: str, arguments: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
        if self._session is None or self._loop is None:
            raise Skip("the MCP server is not running")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, arguments, read_timeout_seconds=timedelta(seconds=timeout)),
            self._loop,
        )
        res = future.result(timeout + 30)
        if isinstance(res.structuredContent, dict):
            return dict(res.structuredContent)
        for item in res.content:
            text = getattr(item, "text", "")
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
        raise Fail(f"{tool} returned no JSON result")

    def tool_names(self) -> list[str]:
        if self._session is None or self._loop is None:
            return []
        future = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        return [t.name for t in future.result(60).tools]

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread.is_alive():
            self._thread.join(15)


class LineReader:
    """Lines of a child process, read on a thread so that waiting for one can time out."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []

        def pump() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                self.lines.put(line.rstrip("\n"))

        threading.Thread(target=pump, daemon=True).start()

    def wait_for(self, needle: str, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            self.seen.append(line)
            if needle in line:
                return line
        raise Fail(f"no line with {needle!r} within {seconds:.0f} seconds; output so far: {self.seen[-5:]}")


def stop_process(proc: subprocess.Popen[str] | None) -> None:
    """Stop a process this script started, by its own handle (its PID)."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)


# Steps


def step_preflight(ctx: Ctx) -> None:
    status = ctx.bridge.status()
    ctx.note("bridge version", status.get("version", "unknown"))
    ctx.note("connected / logged in", f"{status.get('connected')} / {status.get('logged_in')}")
    ctx.note("account (masked by the bridge)", status.get("account") or "empty")
    ctx.note("sending enabled", status.get("sending_enabled"))
    ctx.note("protection", json.dumps(status.get("protection", {}), sort_keys=True))
    ctx.note("security alert", status.get("security_alert"))
    ctx.note("api address", ctx.cfg.api.listen)
    ctx.note("store folder", ctx.cfg.store_dir.name)
    ctx.state["sending_enabled"] = bool(status.get("sending_enabled"))
    if not (status.get("connected") and status.get("logged_in")):
        raise Fail("the bridge is not connected and logged in: link the account first (step login)")
    number = str(ctx.args.self_number or "")
    if not number:
        number = ctx.ask("Phone number of the test account with country code, digits only:")
    digits = "".join(c for c in number if c.isdigit())
    if not matches_masked(digits, str(status.get("account", ""))):
        raise Fail(
            "the number does not fit the masked account of the bridge (first digit and last four digits)"
        )
    ctx.state["own"] = digits
    ctx.note("own number", mask_number(digits))


def step_login(ctx: Ctx) -> None:
    ctx.say(
        "This step records a login test you did by hand. To repeat it: stop the bridge, remove the device"
    )
    ctx.say("under Linked devices on the phone, use a new test store folder and start the bridge.")
    ctx.say(f"Drop screenshots into {ctx.run_dir / 'screenshots'} and name them login-*.png.")
    questions = {
        "QR code scan linked the account": "Did you link the account by scanning the QR code in the login window?",
        "expired code can be reloaded": "Did an expired code offer 'Click to reload QR code' and give a new code?",
        "pairing by phone number": "Did the Phone number tab show an 8-character code that linked the account?",
        "window closed after Connected": "Did the window show Connected and close by itself?",
    }
    answers = {key: ctx.yes_no_skip(question) for key, question in questions.items()}
    for key, answer in answers.items():
        ctx.note(key, {"y": "yes", "n": "no", "s": "not tested in this run"}[answer])
    status = ctx.bridge.status()
    ctx.note("status now", f"logged_in={status.get('logged_in')} connected={status.get('connected')}")
    if "n" in answers.values():
        raise Fail("a login check did not work as described")
    if all(a == "s" for a in answers.values()):
        raise Skip("no login check was run in this session")


def step_chats(ctx: Ctx) -> None:
    chats = ctx.bridge.chats(limit=50)
    ctx.note("chats returned", len(chats))
    if not chats:
        raise Fail("no chats: let the bridge sync history after linking and run again")
    newest = chats[0]
    ctx.note("newest chat", f"{newest.get('jid')} (group={bool(newest.get('is_group'))})")
    jid = str(newest["jid"])
    page = ctx.bridge.messages(chat=jid, limit=10)
    ctx.note("messages in newest chat (page of 10)", len(page))
    times = [int(m.get("sent_at", 0)) for m in page]
    if times != sorted(times, reverse=True):
        raise Fail("messages are not newest first")
    older = ctx.bridge.messages(chat=jid, limit=10, offset=10)
    ctx.note("second page", len(older))
    if older and {m["msg_id"] for m in older} & {m["msg_id"] for m in page}:
        raise Fail("the second page repeats messages of the first page")
    oldest_first = ctx.bridge.messages(chat=jid, limit=5, ascending=True)
    ctx.note("oldest first, first message time", oldest_first[0].get("sent_at") if oldest_first else "none")
    ctx.state["history_chat"] = jid


def step_send_self(ctx: Ctx) -> None:
    ctx.sending_on()
    own = ctx.own()
    text = f"{ctx.marker()} self check {NON_LATIN_WORD}"
    ctx.confirm_self(
        "three sends: one with human-like pacing, one without, and one repeat that must be refused"
    )
    cfg = ctx.cfg.sending
    started = time.monotonic()
    first = ctx.bridge.send(own, text=text, human=True)
    wall = time.monotonic() - started
    if first.get("queued"):
        raise Skip(
            f"quiet hours are active: the send was queued as scheduled entry {first.get('scheduled_id')}"
        )
    waited = float(first.get("waited_ms", 0)) / 1000
    ctx.note("message id", first.get("id"))
    ctx.note("wall time for the human-like send", f"{wall:.1f} s")
    ctx.note("waited_ms reported by the bridge", first.get("waited_ms", 0))
    if cfg.human_like:
        low, high = expected_wait_window(cfg, len(text))
        ctx.note("expected wait window for this text", f"{low:.1f} to {high:.1f} s")
        if not low <= waited <= high:
            raise Fail(
                f"the human-like wait of {waited:.1f} s is outside the expected {low:.1f} to {high:.1f} s"
            )
        if wall + 0.5 < waited:
            raise Fail("the call returned before the reported wait had passed")
    else:
        ctx.note("human_like in config", "false, so no wait is expected")
        if waited > 1:
            raise Fail("sending.human_like is false but the bridge reported a wait")
    started = time.monotonic()
    quick = ctx.bridge.send(own, text=f"{text} (no waits)", human=False)
    ctx.note(
        "send with human=false", f"{time.monotonic() - started:.1f} s, waited_ms={quick.get('waited_ms', 0)}"
    )
    if float(quick.get("waited_ms", 0)) > 1000:
        raise Fail("a send with human=false still waited")
    try:
        ctx.bridge.send(own, text=text, human=False)
    except BridgeError as exc:
        ctx.note("repeat of the first text", f"refused with status {exc.status}")
        if exc.status != 409:
            raise Fail(f"the repeat should be refused with 409, got {exc.status}") from exc
    else:
        raise Fail("the same text to the same chat was sent twice inside the duplicate window")

    def stored() -> dict[str, Any] | None:
        return next(
            (m for m in ctx.bridge.messages(query=ctx.marker(), limit=20) if m["msg_id"] == first["id"]), None
        )

    message = ctx.wait_for(stored, 30, "the sent message to appear in the store", every=2)
    if not message:
        raise Fail("the sent message did not appear in the store within 30 seconds")
    ctx.note("stored as outgoing", message.get("outgoing"))
    ctx.state["self_msg"] = {"id": first["id"], "conv": message["conv"], "text": text}


def step_search(ctx: Ctx) -> None:
    sent = ctx.state.get("self_msg")
    if not sent:
        raise Skip("needs the message from step send_self")
    for label, query in (
        ("lowercase non-Latin word", NON_LATIN_WORD),
        ("uppercase non-Latin word", NON_LATIN_WORD.upper()),
        ("run marker", ctx.marker().upper()),
    ):
        hits = ctx.bridge.messages(query=query, limit=20)
        found = any(m["msg_id"] == sent["id"] for m in hits)
        ctx.note(f"HTTP API search, {label}", f"{len(hits)} hits, test message found={found}")
        if not found:
            raise Fail(f"the API search for the {label} did not find the test message")
    result = ctx.mcp_call("list_messages", {"query": NON_LATIN_WORD.upper(), "limit": 20})
    found = any(m.get("id") == sent["id"] for m in result.get("messages", []))
    ctx.note(
        "MCP list_messages, uppercase non-Latin word",
        f"{result.get('count')} hits, test message found={found}",
    )
    if not found:
        raise Fail("list_messages did not find the test message with an uppercase non-Latin query")


def step_media(ctx: Ctx) -> None:
    own = ctx.own()
    ctx.say("From your phone, send two things to your own chat (message yourself):")
    ctx.say("  1. a screenshot or photo that contains some printed text,")
    ctx.say("  2. a voice note of you saying a few words.")
    ctx.say(f"Waiting up to {ctx.args.wait_seconds} seconds. Press Ctrl+C to stop waiting.")
    since = int(time.time()) - 60

    def seen(kinds: tuple[str, ...]) -> dict[str, Any] | None:
        for m in ctx.bridge.messages(chat=own, after=since, limit=100):
            if m.get("kind") in kinds:
                return m
        return None

    image = ctx.wait_for(lambda: seen(("image",)), float(ctx.args.wait_seconds), "an image")
    voice = ctx.wait_for(lambda: seen(("voice", "audio")), float(ctx.args.wait_seconds), "a voice note")
    ctx.note("image found", bool(image))
    ctx.note("voice note found", bool(voice))
    if not image and not voice:
        raise Skip("no image and no voice note arrived in the self chat")
    for label, message in (("image", image), ("voice note", voice)):
        if not message:
            continue
        saved = ctx.bridge.download_media(str(message["conv"]), str(message["msg_id"]))
        path = Path(str(saved.get("path", "")))
        if not path.is_file():
            raise Fail(f"the {label} download reported {path} but the file is not there")
        with path.open("rb") as stream:
            head = stream.read(12)
        ctx.note(f"{label} saved", f"downloads/{shown_path(ctx, path)} ({path.stat().st_size} bytes)")
        if path.stat().st_size == 0 or not sniff_ok(str(message["kind"]), head):
            raise Fail(f"the saved {label} is empty or has the wrong file type")
        ctx.state["image" if label == "image" else "voice"] = message


def step_transcribe(ctx: Ctx) -> None:
    voice = ctx.state.get("voice")
    if not voice:
        raise Skip("needs a voice note from step media")
    result = ctx.mcp_call(
        "transcribe_chat_audio", {"chat": voice["conv"], "days": 1, "limit": 5, "time_budget_seconds": 300}
    )
    ctx.note(
        "clips transcribed / already done / failed",
        f"{len(result.get('transcribed', []))} / {result.get('already_done')} / {len(result.get('failed', []))}",
    )
    if result.get("failed"):
        ctx.note("first failure", result["failed"][0])
    text = ctx.mcp_call("get_media_text", {"chat": voice["conv"], "message_id": voice["msg_id"]})
    items = [i for i in text.get("items", []) if i.get("source") == "transcript"]
    if not items:
        raise Fail("no transcript was stored for the voice note")
    transcript = str(items[0].get("text", ""))
    ctx.note("language", items[0].get("lang"))
    ctx.note("transcript", code(transcript[:200]))
    spoken = str(ctx.args.spoken or "") or ctx.ask("Type the words you said in the voice note:")
    score = word_overlap(spoken, transcript)
    ctx.note("words of your sentence found in the transcript", f"{score:.0%}")
    if score < 0.6:
        raise Fail("less than 60 percent of the spoken words are in the transcript")


def step_ocr(ctx: Ctx) -> None:
    image = ctx.state.get("image")
    if not image:
        raise Skip("needs an image from step media")
    stamp = datetime.fromtimestamp(ctx.started_at - 3600).isoformat(timespec="seconds")
    result = ctx.mcp_call(
        "read_images", {"chat": image["conv"], "since": stamp, "limit": 5, "time_budget_seconds": 300}
    )
    ctx.note(
        "images read / already done / failed",
        f"{len(result.get('read', []))} / {result.get('already_done')} / {len(result.get('failed', []))}",
    )
    text = ctx.mcp_call("get_media_text", {"chat": image["conv"], "message_id": image["msg_id"]})
    items = [i for i in text.get("items", []) if i.get("source") == "image"]
    if not items:
        raise Fail(
            f"no text was read from the image ({text.get('hint', 'check get_bridge_status for ocr_ready')})"
        )
    ocr_text = str(items[0].get("text", ""))
    ctx.note("text read", code(" ".join(ocr_text.split())[:200]))
    word = str(ctx.args.image_word or "") or ctx.ask("Type one word that is printed in the screenshot:")
    if word.casefold() not in ocr_text.casefold():
        raise Fail("the word you typed is not in the text that OCR read")
    found = ctx.mcp_call("list_messages", {"query": word, "limit": 20})
    hit = any(m.get("id") == image["msg_id"] for m in found.get("messages", []))
    ctx.note("search for that word finds the image message", hit)
    if not hit:
        raise Fail("search did not find the image by its text")


def step_import(ctx: Ctx) -> None:
    folder = ctx.cfg.exports_dir / "e2e" / ctx.run_id
    path, count = make_test_export(folder, ctx.marker())
    ctx.note(
        "test export",
        f"{path.name}, {count} messages, for the fictional chat {mask_number(TEST_CHAT_NUMBER)}",
    )
    ctx.say("This step imports a generated file into a local chat for a fictional number. Nothing is sent.")
    options = {"me": "E2E Me", "timezone": "UTC", "date_order": "dmy"}
    me, zone, order = options["me"], options["timezone"], options["date_order"]
    dry = ctx.bridge.import_export(str(path), TEST_CHAT_NUMBER, me, order, True, zone)
    ctx.note("dry run: messages_added", dry.get("messages_added"))
    if dry.get("messages_added") != count:
        raise Fail(f"the dry run would store {dry.get('messages_added')} messages, expected {count}")
    first = ctx.bridge.import_export(str(path), TEST_CHAT_NUMBER, me, order, False, zone)
    ctx.note("import: messages_added", first.get("messages_added"))
    if first.get("messages_added") != count:
        raise Fail(f"the import stored {first.get('messages_added')} messages, expected {count}")
    again = ctx.bridge.import_export(str(path), TEST_CHAT_NUMBER, me, order, False, zone)
    ctx.note(
        "second import: messages_added / already_imported",
        f"{again.get('messages_added')} / {again.get('already_imported')}",
    )
    if again.get("messages_added") != 0:
        raise Fail("importing the same file again added messages")
    stored = ctx.bridge.messages(chat=TEST_CHAT_NUMBER, limit=50)
    ctx.note("messages stored in the test chat", len(stored))
    hits = ctx.bridge.messages(chat=TEST_CHAT_NUMBER, query=NON_LATIN_WORD.upper(), limit=10)
    ctx.note("non-Latin search in the imported chat", f"{len(hits)} hits")
    if not hits:
        raise Fail("the non-Latin word of the export was not found by search")
    ctx.note("clean up", "the fictional chat stays in the store; see docs/testing/e2e.md to remove it")


def step_reply_group(ctx: Ctx) -> None:
    ctx.sending_on()
    group = ctx.group()
    own = ctx.own()
    mention = "".join(c for c in str(ctx.args.mention or own) if c.isdigit())
    first_text = f"{ctx.marker()} group check"
    ctx.confirm_visible(
        f"send {first_text!r}, then a reply to it that mentions {mask_number(mention)}", group
    )
    first = ctx.bridge.send(group, text=first_text, human=True)
    ctx.note("first message id", first.get("id"))
    reply = ctx.bridge.reply(
        group, str(first["id"]), f"{ctx.marker()} group reply", mentions=[mention], human=True
    )
    ctx.note("reply id", reply.get("id"))

    def stored() -> dict[str, Any] | None:
        return next(
            (
                m
                for m in ctx.bridge.messages(chat=group, query=ctx.marker(), limit=20)
                if m["msg_id"] == reply["id"]
            ),
            None,
        )

    message = ctx.wait_for(stored, 30, "the reply to appear in the store", every=2)
    if not message:
        raise Fail("the reply did not appear in the store within 30 seconds")
    ctx.note("reply_to_id stored", message.get("reply_to_id") == first["id"])
    ctx.note("mentioned stored", json.dumps(message.get("mentioned", [])))
    if message.get("reply_to_id") != first["id"]:
        raise Fail("the stored reply does not point at the first message")
    if not any(mention in str(j) for j in message.get("mentioned", [])):
        raise Fail("the mention was not stored with the reply")
    ctx.say(
        f"Check in WhatsApp that the reply shows the quote and highlights the mention. Screenshot: reply_group-*.png in {ctx.run_dir / 'screenshots'}"
    )
    ctx.state["group_sent"] = str(first["id"])


def step_mark_read(ctx: Ctx) -> None:
    group = ctx.group()

    def incoming() -> dict[str, Any] | None:
        return next((m for m in ctx.bridge.messages(chat=group, limit=50) if not m.get("outgoing")), None)

    target = incoming()
    if target is None:
        ctx.say("The test group has no message from someone else. Ask a member to post one now.")
        target = ctx.wait_for(incoming, float(ctx.args.wait_seconds), "a message from another member")
    if target is None:
        raise Skip("no incoming message in the test group")
    ctx.confirm_visible("mark one incoming message as read (the sender can see the blue ticks)", group)
    try:
        result = ctx.bridge.mark_read(
            group, [str(target["msg_id"])], author=str(target.get("author") or "") or None
        )
    except BridgeError as exc:
        if exc.status == 403:
            raise Skip("read_receipts.allow_mark_read is false") from exc
        raise
    ctx.note("message id", target["msg_id"])
    ctx.note("marked", result.get("marked"))
    if int(result.get("marked", 0)) < 1:
        raise Fail("the bridge reported no message as read")


def step_schedule(ctx: Ctx) -> None:
    ctx.sending_on()
    own = ctx.own()
    ctx.confirm_self(
        "schedule one message 2 minutes ahead, wait for its delivery, then schedule one and cancel it"
    )
    due = int(time.time()) + 120
    text = f"{ctx.marker()} scheduled"
    made = ctx.bridge.schedule(own, due, text=text)
    sid = int(made["id"])
    ctx.note("scheduled id / due", f"{sid} / {datetime.fromtimestamp(due).isoformat(timespec='seconds')}")

    def entry() -> dict[str, Any] | None:
        row = next((e for e in ctx.bridge.list_scheduled() if int(e["id"]) == sid), None)
        return row if row and row.get("status") in ("sent", "failed", "cancelled") else None

    ctx.say("  waiting up to 5 minutes for the delivery")
    final = ctx.wait_for(entry, 300, "the scheduled message to be sent", every=5)
    if not final:
        raise Fail(
            "the scheduled message was not sent within 5 minutes (is the bridge connected and sending on?)"
        )
    ctx.note("final status", final.get("status"))
    sent_at = int(final.get("last_sent_at") or 0)
    ctx.note("seconds after the due time", sent_at - due)
    if final.get("status") != "sent":
        raise Fail(f"the scheduled message ended as {final.get('status')}: {final.get('last_error', '')}")
    if not 0 <= sent_at - due <= 120:
        raise Fail("the message was sent more than 2 minutes after its time (or before it)")
    stored = ctx.bridge.messages(query=text, limit=10)
    ctx.note("delivered message found in the store", bool(stored))
    later = ctx.bridge.schedule(own, int(time.time()) + 3600, text=f"{ctx.marker()} to be cancelled")
    ctx.bridge.cancel_scheduled(int(later["id"]))
    state = next((e for e in ctx.bridge.list_scheduled() if int(e["id"]) == int(later["id"])), {})
    ctx.note("cancelled entry status", state.get("status"))
    if state.get("status") != "cancelled":
        raise Fail("a cancelled scheduled message is not marked cancelled")


def step_calls(ctx: Ctx) -> None:
    if not ctx.cfg.raw.get("calls", {}).get("record", True):
        raise Skip("calls.record is false")
    since = int(time.time()) - 10
    ctx.say("Call the linked account from a second phone. Answer or reject the incoming call, then hang up.")

    def newest() -> dict[str, Any] | None:
        rows = ctx.bridge.calls(since=since, limit=10)
        return rows[0] if rows else None

    call = ctx.wait_for(newest, float(ctx.args.wait_seconds), "a call to be recorded")
    if not call:
        raise Fail("no call appeared in the call log")
    ctx.note("call id", call.get("call_id"))
    ctx.note(
        "outcome / video / outgoing", f"{call.get('outcome')} / {call.get('video')} / {call.get('outgoing')}"
    )
    listed = ctx.mcp_call("list_calls", {"since": since, "limit": 10})
    ctx.note("MCP list_calls count", len(listed.get("calls", [])))
    if not listed.get("calls"):
        raise Fail("list_calls returned nothing although the API has a call")


def bridge_connected(ctx: Ctx) -> bool:
    try:
        return bool(ctx.bridge.status().get("connected"))
    except BridgeError:
        return False


def shown_path(ctx: Ctx, path: Path) -> str:
    """A path without the user's home folder: relative to downloads_dir or exports_dir when possible."""
    for base in (ctx.cfg.downloads_dir, ctx.cfg.exports_dir):
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return path.name


def step_webhook(ctx: Ctx) -> None:
    ctx.sending_on()
    own = ctx.own()
    secret = os.environ.get(WEBHOOK_VAR, "")
    generated = not secret
    secret = secret or secrets.token_hex(32)
    log = ctx.run_dir / "webhooks.jsonl"
    env = {**os.environ, WEBHOOK_VAR: secret, "PYTHONUNBUFFERED": "1"}
    cmd = [sys.executable, str(RECEIVER), "--port", "0", "--jsonl", str(log)]
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace"
    )
    try:
        line = LineReader(proc).wait_for("listening on", 30)
        port = int(line.split("http://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        ctx.note("receiver", f"127.0.0.1:{port}, pid {proc.pid}, log {log.name}")
        snippet = {"url": f"http://127.0.0.1:{port}/hook", "events": ["message"], "secret_env": WEBHOOK_VAR}
        ctx.say("Add this entry to the webhooks list in config.json and restart the bridge:")
        ctx.say("  " + json.dumps(snippet))
        if generated:
            ctx.say(
                f"The bridge needs the same secret in {WEBHOOK_VAR}. Set it in the shell that starts the bridge:"
            )
            ctx.say(f'  PowerShell:  $env:{WEBHOOK_VAR} = "{secret}"')
            ctx.say(f'  bash or zsh: export {WEBHOOK_VAR}="{secret}"')
            ctx.say("(the secret is shown only here and is not written to the report)")
        ctx.pause("Restart the bridge, wait until it is connected again.")
        back = ctx.wait_for(lambda: bridge_connected(ctx), 180, "the bridge to be connected", every=3)
        if not back:
            raise Fail("the bridge did not come back connected within 3 minutes")
        url = f"http://127.0.0.1:{port}/hook"
        with httpx.Client(timeout=10, trust_env=False) as http:
            forged = http.post(
                url,
                content=b"{}",
                headers={
                    "X-WA-MCP-Timestamp": str(int(time.time())),
                    "X-WA-MCP-Delivery": "forged",
                    "X-WA-MCP-Signature": "sha256=" + "0" * 64,
                },
            )
        ctx.note("receiver answer to a forged signature", forged.status_code)
        if forged.status_code != 401:
            raise Fail("the receiver accepted a forged signature")
        ctx.confirm_self("send one message to your own number to trigger a message event")
        sent = ctx.bridge.send(own, text=f"{ctx.marker()} webhook", human=False)

        def delivered() -> dict[str, Any] | None:
            if not log.is_file():
                return None
            for raw in log.read_text(encoding="utf-8").splitlines():
                record = json.loads(raw)
                if record.get("msg_id") == sent["id"] and record.get("event") == "message":
                    return dict(record)
            return None

        record = ctx.wait_for(delivered, 90, "the webhook delivery", every=2)
        if not record:
            raise Fail("no signed delivery for the test message arrived within 90 seconds")
        ctx.note("delivery id", record["delivery"])
        ctx.note(
            "event / age at receipt", f"{record['event']} / {record['received_at'] - record['timestamp']} s"
        )
        ctx.note("signature and timestamp window", "verified by the receiver before it logged the delivery")
        ctx.say(
            f"Remove the webhook entry from config.json and restart the bridge when you are done. Receiver pid {proc.pid} is stopped now."
        )
    finally:
        stop_process(proc)


def _manage_token(ctx: Ctx, action: str, name: str) -> str:
    cmd = [sys.executable, "-m", "wa_mcp_server", "token", action, "--config", str(ctx.cfg.file)]
    cmd += ["--name", name] if action == "create" else [name]
    done = subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False
    )
    if done.returncode != 0:
        raise Fail(f"wa-mcp token {action} failed: {redact(done.stderr.strip())}")
    return done.stdout


def step_http_mcp(ctx: Ctx) -> None:
    name = f"e2e-{ctx.run_id}"
    ctx.confirm_local(
        f"create a token named {name}, run an MCP server on a random loopback port, and revoke the token at the end"
    )
    proc: subprocess.Popen[str] | None = None
    revoked = False
    try:
        out = _manage_token(ctx, "create", name)
        match = re.search(r"\bwam_[A-Za-z0-9_-]+", out)
        if not match:
            raise Fail("could not read the new token from the output")
        token = match.group(0)
        port = free_port()
        cmd = [
            sys.executable,
            "-m",
            "wa_mcp_server",
            "serve",
            "--http",
            "--listen",
            f"127.0.0.1:{port}",
            "--config",
            str(ctx.cfg.file),
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace"
        )
        LineReader(proc)
        base = f"http://127.0.0.1:{port}"
        ctx.note("server", f"127.0.0.1:{port}, pid {proc.pid}")
        with httpx.Client(timeout=20, trust_env=False) as http:
            up = ctx.wait_for(lambda: _health(http, base), 40, "the HTTP server to start", every=1)
            if not up:
                raise Fail("the HTTP MCP server did not answer /health within 40 seconds")
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e-check", "version": "1"},
                },
            }
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            missing = http.post(f"{base}/mcp", json=init, headers=headers)
            wrong = http.post(
                f"{base}/mcp", json=init, headers={**headers, "Authorization": "Bearer wam_" + "x" * 40}
            )
            ctx.note("without a token", missing.status_code)
            ctx.note("with a wrong token", wrong.status_code)
            if missing.status_code != 401 or wrong.status_code != 401:
                raise Fail("a request without a valid token was not answered with 401")
            auth = {**headers, "Authorization": f"Bearer {token}"}
            ok = http.post(f"{base}/mcp", json=init, headers=auth)
            ctx.note("initialize with the token", ok.status_code)
            if ok.status_code != 200:
                raise Fail(f"initialize with a valid token answered {ok.status_code}")
            info = parse_sse_or_json(ok).get("result", {}).get("serverInfo", {})
            ctx.note("server name", info.get("name"))
            session = ok.headers.get("mcp-session-id")
            auth2 = {**auth, **({"Mcp-Session-Id": session} if session else {})}
            http.post(
                f"{base}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth2
            )
            listing = http.post(
                f"{base}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth2
            )
            tools = parse_sse_or_json(listing).get("result", {}).get("tools", [])
            ctx.note("tools listed", len(tools))
            if not tools:
                raise Fail("tools/list returned no tools")
            call = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_bridge_status", "arguments": {}},
            }
            status = parse_sse_or_json(http.post(f"{base}/mcp", json=call, headers=auth2))
            ctx.note("get_bridge_status over HTTP", "answered" if status.get("result") else "no result")
            if not status.get("result"):
                raise Fail("get_bridge_status over HTTP returned no result")
            _manage_token(ctx, "revoke", name)
            revoked = True
            after = http.post(f"{base}/mcp", json=init, headers=auth)
            ctx.note("after revoking the token", after.status_code)
            if after.status_code != 401:
                raise Fail("a revoked token was still accepted")
    finally:
        stop_process(proc)
        if not revoked:
            try:
                _manage_token(ctx, "revoke", name)
            except Fail:
                ctx.note("warning", f"revoke the token {name} by hand: wa-mcp token revoke {name}")


def _health(http: httpx.Client, base: str) -> bool:
    try:
        return http.get(f"{base}/health").status_code == 200
    except httpx.HTTPError:
        return False


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=False
    )


def step_docker(ctx: Ctx) -> None:
    if not shutil.which("docker"):
        raise Skip("docker is not installed on this machine")
    listed = _docker(
        "ps", "--filter", "label=com.docker.compose.project=wa-mcp", "--format", "{{.Names}}|{{.Status}}"
    )
    rows = [r.split("|", 1) for r in listed.stdout.splitlines() if "|" in r]
    if listed.returncode != 0 or not rows:
        raise Skip("no running Compose project named wa-mcp (see docs/guides/server.md)")
    for name, status in rows:
        ctx.note(f"container {name}", status)
        if "healthy" not in status or "unhealthy" in status:
            raise Fail(f"container {name} is not healthy: {status}")
    for name, _ in rows:
        info = _docker("inspect", "--format", "{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}", name)
        ctx.note(f"{name} user | read-only root", info.stdout.strip())
        if info.stdout.strip() and not info.stdout.strip().endswith("|true"):
            raise Fail(f"container {name} has a writable root file system")
    bridge = next((n for n, _ in rows if "bridge" in n), "")
    if bridge:
        health = _docker("exec", bridge, "wa-bridge", "health")
        ctx.note("wa-bridge health", f"exit {health.returncode}")
        if health.returncode != 0:
            raise Fail("wa-bridge health failed inside the container")
    port = os.environ.get("WA_MCP_PORT", "8765") or "8765"
    with httpx.Client(timeout=10, trust_env=False) as http:
        try:
            answer = http.get(f"http://127.0.0.1:{port}/health")
            ctx.note(f"GET /health on 127.0.0.1:{port}", answer.status_code)
            if answer.status_code != 200:
                raise Fail("the server health endpoint did not answer 200")
        except httpx.HTTPError as exc:
            raise Fail(f"the server health endpoint is not reachable on port {port}: {exc}") from exc


SEALED_COLUMNS = {
    "chat_messages": ("body", "attachment_name", "reply_preview", "media_key"),
    "pending_changes": ("body",),
    "reactions": ("emoji",),
    "scheduled_messages": ("body",),
    "derived_text": ("content",),
    "wa_contacts": ("first_name", "full_name", "push_name", "business_name"),
}


def step_encryption(ctx: Ctx) -> None:
    mode = ctx.cfg.encryption.mode
    ctx.note("encryption.mode", mode)
    if mode == "off":
        raise Skip("encryption.mode is off")
    source = ctx.cfg.wa_db
    if not source.is_file():
        raise Skip(f"no database at {source.name} (the bridge runs somewhere else?)")
    tmp = Path(tempfile.mkdtemp(prefix="wa-mcp-e2e-enc-"))
    try:
        copy = tmp / "wa.db"
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        con = sqlite3.connect(copy)
        try:
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            if not meta.get("crypt_salt"):
                raise Fail("the database has no encryption salt: message text is stored in plain form")
            for table, columns in SEALED_COLUMNS.items():
                for column in columns:
                    rows = con.execute(
                        f"SELECT {column} FROM {table} WHERE length(CAST({column} AS BLOB)) > 0 LIMIT 200"
                    ).fetchall()
                    sealed = sum(crypt.is_sealed(value) for (value,) in rows)
                    ctx.note(f"{table}.{column} sealed", f"{sealed} of {len(rows)} sampled")
                    if sealed != len(rows):
                        raise Fail(f"{table}.{column} contains values in plain form")
        finally:
            con.close()
        kind = str(meta.get("crypt_kind") or "passphrase")
        ctx.note("key kind recorded in the database", kind)
        with copy.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            for label, needle in (("run marker", ctx.marker()), ("non-Latin test word", NON_LATIN_WORD)):
                present = raw.find(needle.encode("utf-8")) >= 0
                ctx.note(f"{label} readable in the copied file", present)
                if present:
                    raise Fail(f"the {label} can be read as plain text in the copied database")
        _open_with_wrong_key(ctx, tmp, kind)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ctx.note("temporary copy", "deleted")


def _open_with_wrong_key(ctx: Ctx, tmp: Path, kind: str) -> None:
    enc: dict[str, Any] = {
        "mode": ctx.cfg.encryption.mode,
        "passphrase_env": ctx.cfg.encryption.passphrase_env,
    }
    if kind == "keyfile":
        wrong = tmp / "wrong.key"
        wrong.write_text(secrets.token_hex(32), encoding="utf-8")
        wrong.chmod(0o600)
        enc["mode"] = "keyfile"
        enc["key_file"] = str(wrong)
    config_file = tmp / "config.json"
    config_file.write_text(json.dumps({"store_dir": str(tmp), "encryption": enc}), encoding="utf-8")
    cfg = wa_config.load(config_file)
    previous = os.environ.get(cfg.encryption.passphrase_env)
    if kind == "passphrase":
        os.environ[cfg.encryption.passphrase_env] = "not the passphrase of this store"
    try:
        wa_db.Store(cfg).conversations(limit=1)
    except wa_db.StoreError as exc:
        ctx.note("opening the copy with another key", f"refused: {exc}")
    else:
        raise Fail("the copy of the database could be opened with another key")
    finally:
        if kind == "passphrase":
            if previous is None:
                os.environ.pop(cfg.encryption.passphrase_env, None)
            else:
                os.environ[cfg.encryption.passphrase_env] = previous


def step_accounts(ctx: Ctx) -> None:
    names = list(ctx.cfg.account_names)
    ctx.note("accounts in config.json", ", ".join(names))
    if len(names) < 2:
        raise Skip("only the account default is configured")
    listed = ctx.mcp_call("list_accounts", {})
    reported = [str(a.get("name")) for a in listed.get("accounts", [])]
    ctx.note("list_accounts", ", ".join(reported))
    if sorted(reported) != sorted(names):
        raise Fail("list_accounts does not match the accounts in config.json")
    stores = {str(ctx.cfg.for_account(n).store_dir) for n in names}
    ports = {ctx.cfg.for_account(n).api.listen for n in names}
    ctx.note("separate store folders / api addresses", f"{len(stores)} / {len(ports)}")
    if len(stores) != len(names) or len(ports) != len(names):
        raise Fail("two accounts share a store folder or an API address")
    for name in names:
        try:
            with Bridge(cfg=ctx.cfg, account=name) as bridge:
                st = bridge.status()
            ctx.note(
                f"account {name}",
                f"connected={st.get('connected')} logged_in={st.get('logged_in')} number={st.get('account') or 'empty'}",
            )
        except BridgeError as exc:
            ctx.note(f"account {name}", f"bridge not reachable ({exc.message})")


@dataclass(frozen=True)
class StepDef:
    id: str
    title: str
    run: Callable[[Ctx], None]
    needs: str
    effect: str
    visible: bool = False
    manual: bool = False
    optional: bool = False


STEPS: tuple[StepDef, ...] = (
    StepDef(
        "preflight",
        "Bridge status and test account",
        step_preflight,
        "bridge running and linked",
        "reads status",
    ),
    StepDef(
        "login",
        "Login window: QR and phone code",
        step_login,
        "a person",
        "nothing; records your answers",
        manual=True,
    ),
    StepDef("chats", "List chats and read history", step_chats, "synced chats", "reads"),
    StepDef(
        "send_self",
        "Send to yourself with human-like timing",
        step_send_self,
        "sending.enabled",
        "3 sends to your own number, 1 refused",
    ),
    StepDef("search", "Search, including a non-Latin query", step_search, "step send_self", "reads"),
    StepDef(
        "media",
        "Download an image and a voice note",
        step_media,
        "you send both from the phone",
        "saves 2 files to downloads_dir",
        manual=True,
    ),
    StepDef(
        "transcribe",
        "Transcribe the voice note",
        step_transcribe,
        "step media, a speech backend, ffmpeg",
        "stores a transcript",
    ),
    StepDef(
        "ocr", "Read the text of a screenshot", step_ocr, "step media, an OCR engine", "stores image text"
    ),
    StepDef(
        "import",
        "Import a generated test export",
        step_import,
        "bridge running",
        "adds 6 messages to a local fictional chat",
    ),
    StepDef(
        "reply_group",
        "Reply with quote and mention in the test group",
        step_reply_group,
        "--group, sending.enabled",
        "2 group messages",
        visible=True,
    ),
    StepDef(
        "mark_read",
        "Mark a group message as read",
        step_mark_read,
        "--group, a message from another member",
        "sends a read receipt",
        visible=True,
        manual=True,
    ),
    StepDef(
        "schedule",
        "Schedule a message and confirm delivery",
        step_schedule,
        "sending.enabled",
        "1 scheduled send to yourself, 1 cancelled",
    ),
    StepDef("calls", "Call history after a test call", step_calls, "you make a call", "reads", manual=True),
    StepDef(
        "webhook",
        "Webhook delivery to a local receiver",
        step_webhook,
        "a bridge restart with a webhook entry",
        "starts a receiver, 1 send to yourself",
        manual=True,
    ),
    StepDef(
        "http_mcp",
        "HTTP MCP with a token, and 401 without",
        step_http_mcp,
        "none",
        "creates and revokes a token, starts a server",
    ),
    StepDef(
        "docker", "Docker mode", step_docker, "docker and a running wa-mcp project", "reads", optional=True
    ),
    StepDef(
        "encryption",
        "Encryption: a copy cannot be opened",
        step_encryption,
        "encryption on",
        "reads a snapshot, deletes it",
        optional=True,
    ),
    StepDef(
        "accounts",
        "Several accounts",
        step_accounts,
        "an accounts block in config.json",
        "reads",
        optional=True,
    ),
)


def select_steps(only: str, skip: str) -> list[StepDef]:
    known = {s.id for s in STEPS}
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    dropped = {x.strip() for x in skip.split(",") if x.strip()}
    unknown = (wanted | dropped) - known
    if unknown:
        raise ValueError(f"unknown step ids: {', '.join(sorted(unknown))}; see --list")
    return [s for s in STEPS if (not wanted or s.id in wanted) and s.id not in dropped]


def plan_text(steps: list[StepDef], cfg: wa_config.Config, args: argparse.Namespace) -> str:
    lines = [
        "WA MCP end-to-end check: plan",
        "",
        "Nothing is contacted, no file is written and no process is started in --dry-run.",
        "",
        f"config file:        {cfg.file}",
        f"bridge API:         {cfg.api.listen}",
        f"store folder:       {cfg.store_dir}",
        f"downloads / exports: {cfg.downloads_dir} / {cfg.exports_dir}",
        f"run folder (report): {cfg.exports_dir / 'e2e' / '<run id>'}",
        f"sending enabled in config: {cfg.sending.enabled}",
        f"encryption mode:    {cfg.encryption.mode}",
        f"accounts:           {', '.join(cfg.account_names)}",
        f"test group:         {args.group or 'not given (group steps will be skipped)'}",
        "",
        "Steps, in order. Marks: V = visible to other people (asks for the word send), M = needs you at the",
        "keyboard or phone, O = skipped when it does not apply.",
        "",
    ]
    for i, s in enumerate(steps, 1):
        marks = ("V" if s.visible else "-") + ("M" if s.manual else "-") + ("O" if s.optional else "-")
        lines += [f"{i:2}. {s.id:12} [{marks}] {s.title}", f"      needs: {s.needs}; effect: {s.effect}"]
    lines += [
        "",
        "Messages go only to your own number and to the test group you name. Run without --dry-run to start.",
    ]
    return "\n".join(lines) + "\n"


def run_step(step: StepDef, ctx: Ctx) -> StepResult:
    started = datetime.now()
    clock = time.monotonic()
    ctx.evidence = []
    ctx.say(f"\n== {step.id}: {step.title}")
    status, note = PASS, ""
    try:
        step.run(ctx)
    except Skip as exc:
        status, note = SKIPPED, str(exc)
    except Fail as exc:
        status, note = FAIL, str(exc)
    except BridgeError as exc:
        status, note = FAIL, f"bridge request failed: {exc}"
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        status, note = FAIL, f"{exc.__class__.__name__}: {exc}"
    result = StepResult(
        step.id,
        step.title,
        status,
        time.monotonic() - clock,
        list(ctx.evidence),
        note,
        started.strftime("%H:%M:%S"),
    )
    ctx.say(f"   {status}" + (f": {redact(note)}" if note else ""))
    for item in result.evidence:
        ctx.say(f"   - {redact(item)}")
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="End-to-end check of WA MCP with a real test account (see the docstring)"
    )
    p.add_argument("--config", help="path of config.json (default: WA_MCP_CONFIG or ./config.json)")
    p.add_argument(
        "--self-number", help="phone number of the test account with country code (asked when missing)"
    )
    p.add_argument(
        "--group", help="JID or exact title of a test group (without it the group steps are skipped)"
    )
    p.add_argument("--mention", help="number to mention in the group reply (default: the test account)")
    p.add_argument("--spoken", help="the words you say in the voice note (asked when missing)")
    p.add_argument("--image-word", help="a word printed in the screenshot (asked when missing)")
    p.add_argument("--only", default="", help="comma separated step ids to run")
    p.add_argument("--skip", default="", help="comma separated step ids to leave out")
    p.add_argument("--wait-seconds", type=int, default=180, help="how long to wait for things you do by hand")
    p.add_argument("--list", action="store_true", help="list the step ids and exit")
    p.add_argument("--dry-run", action="store_true", help="print the plan and touch nothing")
    return p


def facts_for(ctx: Ctx) -> dict[str, str]:
    cfg = ctx.cfg
    return {
        "Machine": f"{platform.system()} {platform.release()}, Python {platform.python_version()}",
        "wa_mcp SDK": sdk_version,
        "Config": cfg.file.name,
        "Encryption mode": cfg.encryption.mode,
        "Sending enabled in config": str(cfg.sending.enabled),
        "Transcription backend": cfg.transcription.backend,
        "OCR engine": cfg.vision.ocr,
        "Accounts": ", ".join(cfg.account_names),
        "Test group given": "yes" if ctx.args.group else "no",
    }


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parser().parse_args(argv)
    try:
        cfg = wa_config.load(args.config)
        steps = select_steps(args.only, args.skip)
    except (wa_config.ConfigError, ValueError) as exc:
        sys.stderr.write(f"e2e_check: {exc}\n")
        return 2
    if args.list:
        sys.stdout.write("".join(f"{s.id}\t{s.title}\n" for s in STEPS))
        return 0
    if args.dry_run:
        sys.stdout.write(plan_text(steps, cfg, args))
        return 0

    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.exports_dir / "e2e" / run_id
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.md"
    bridge = Bridge(cfg=cfg)
    ctx = Ctx(args=args, cfg=cfg, run_id=run_id, run_dir=run_dir, bridge=bridge)
    report = Report(run_id, started, facts_for(ctx))
    ctx.say(f"Run {run_id}. Report: {report_path}")
    ctx.say(
        f"Drop screenshots into {run_dir / 'screenshots'}; name them after the step, for example login-qr.png."
    )
    ctx.say("Messages go only to your own number and to the test group you named. Ctrl+C stops the run.")
    server = McpStdio(cfg.file)
    try:
        server.start()
        ctx.mcp = server
    except Skip as exc:
        ctx.say(f"MCP server not available, steps that need it will skip: {exc}")
    try:
        for step in steps:
            result = run_step(step, ctx)
            report.add(result)
            report.write(report_path, list_files(run_dir / "screenshots"))
            if step.id == "preflight" and result.status == FAIL:
                ctx.say(
                    "\nThe preflight failed, so the other steps are not run. Fix the setup and start again."
                )
                break
    except (Interrupted, KeyboardInterrupt):
        ctx.say("\nStopped by the person running the check. The report has the steps done so far.")
    finally:
        report.finished = datetime.now()
        server.close()
        bridge.close()
        report.write(report_path, list_files(run_dir / "screenshots"))
    c = report.counts()
    ctx.say(f"\n{report.verdict()}: {c[PASS]} passed, {c[FAIL]} failed, {c[SKIPPED]} skipped")
    ctx.say(f"Report: {report_path}")
    return 1 if c[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
