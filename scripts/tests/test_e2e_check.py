"""Tests for scripts/e2e_check.py. No WhatsApp account and no network: a fake bridge stands in for the real one."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import e2e_check as e2e
import pytest

from wa_mcp import config as wa_config
from wa_mcp import crypt
from wa_mcp.client import BridgeError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))
import synth

OWN = "15550100001"
GROUP = "120363000000000001@g.us"


# helpers


def make_cfg(tmp_path: Path, **extra: Any) -> wa_config.Config:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": {"token": "t" * 40}, **extra}), encoding="utf-8")
    return wa_config.load(path)


class FakeBridge:
    """Records every send. Answers like the real bridge for the calls the steps make."""

    def __init__(self, clock: list[float], waited_ms: int = 9000, own: str = OWN) -> None:
        self.clock = clock
        self.waited_ms = waited_ms
        self.own = own
        self.sent: list[dict[str, Any]] = []
        self.stored: list[dict[str, Any]] = []
        self.refuse_duplicates = True

    def status(self) -> dict[str, Any]:
        return {"connected": True, "logged_in": True, "account": "+1 ••• ••• 0001", "sending_enabled": True}

    def send(self, to: str, text: str | None = None, human: bool = True, **kw: Any) -> dict[str, Any]:
        if self.refuse_duplicates and any(s["to"] == to and s["text"] == text for s in self.sent):
            raise BridgeError(409, "duplicate")
        waited = self.waited_ms if human else 0
        self.clock[0] += waited / 1000
        msg_id = f"ID{len(self.sent) + 1}"
        self.sent.append({"to": to, "text": text, "human": human, "id": msg_id, **kw})
        self.stored.append({"conv": f"{to}", "msg_id": msg_id, "body": text, "outgoing": True, **kw})
        return {"id": msg_id, "ok": True, "waited_ms": waited}

    def reply(
        self, to: str, reply_to_id: str, text: str, mentions: list[str] | None = None, human: bool = True
    ) -> dict[str, Any]:
        out = self.send(
            to,
            text=text,
            human=human,
            reply_to_id=reply_to_id,
            mentioned=[f"{m}@s.whatsapp.net" for m in mentions or []],
        )
        return out

    def messages(self, chat: str | None = None, query: str | None = None, **kw: Any) -> list[dict[str, Any]]:
        rows = [m for m in self.stored if not query or query.casefold() in str(m["body"]).casefold()]
        return [m for m in rows if chat is None or m["conv"] == chat or chat.endswith("@g.us")]


def make_ctx(tmp_path: Path, bridge: FakeBridge, *argv: str, answers: list[str] | None = None) -> e2e.Ctx:
    args = e2e.parser().parse_args(list(argv))
    ctx = e2e.Ctx(
        args=args,
        cfg=make_cfg(tmp_path),
        run_id="20261123-100000",
        run_dir=tmp_path / "run",
        bridge=cast(Any, bridge),
    )
    queue = list(answers or [])
    ctx._read = lambda prompt: queue.pop(0) if queue else ""  # type: ignore[method-assign]
    ctx.say = lambda text="": None  # type: ignore[method-assign]
    return ctx


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(e2e.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(e2e.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


# masking and small helpers


def test_redact_masks_numbers_folders_and_tokens() -> None:
    text = "chat 15550100002@s.whatsapp.net in downloads/15550100002_s.whatsapp.net/a.jpg group 120363000000000001@g.us"
    out = e2e.redact(text)
    assert "15550100002" not in out and "120363000000000001" not in out
    assert "1***0002@s.whatsapp.net" in out and "1***0002_s.whatsapp.net" in out
    assert e2e.redact("token wam_abcdefghijklmnop123456 end") == "token wam_[hidden] end"
    assert e2e.redact("key " + "ab" * 32) == "key [hidden]"
    assert e2e.redact("sent_at 1795426198 id 3EB0A1B2C3") == "sent_at 1795426198 id 3EB0A1B2C3"


def test_masked_account_matching() -> None:
    assert e2e.redact("+1 \u2022\u2022\u2022 \u2022\u2022\u2022 0101") == "+1 *** *** 0101"
    assert e2e.matches_masked("15550100001", "+1 ••• ••• 0001")
    assert not e2e.matches_masked("15550100002", "+1 ••• ••• 0001")
    assert not e2e.matches_masked("25550100001", "+1 ••• ••• 0001")
    assert not e2e.matches_masked("12", "+1 ••• ••• 0001")
    assert e2e.mask_number("+1 555 010 0001") == "1***0001"


def test_wait_window_follows_config(tmp_path: Path) -> None:
    sending = make_cfg(tmp_path).sending
    low, high = e2e.expected_wait_window(sending, 60)
    assert 5.0 < low < 7.0 and 14.0 < high < 20.0  # 60 characters at 7 per second, plus or minus 30 percent
    short_low, _ = e2e.expected_wait_window(sending, 3)
    assert short_low < low
    slow = make_cfg(tmp_path, sending={"typing_chars_per_second": 3, "typing_max_seconds": 5})
    assert e2e.expected_wait_window(slow.sending, 600)[1] < 20


def test_word_overlap() -> None:
    assert e2e.word_overlap("testing one two three", "Testing, one two three.") == 1.0
    assert e2e.word_overlap("testing one two three", "something else") == 0.0
    assert e2e.word_overlap("", "anything") == 0.0


def test_parse_sse_or_json() -> None:
    import httpx

    sse = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"result": {"a": 1}}\n\n',
    )
    assert e2e.parse_sse_or_json(sse) == {"result": {"a": 1}}
    plain = httpx.Response(200, json={"result": 2})
    assert e2e.parse_sse_or_json(plain) == {"result": 2}


def test_test_export_matches_the_documented_layout(tmp_path: Path) -> None:
    path, count = e2e.make_test_export(tmp_path / "x", "wa-mcp-e2e-1")
    text = path.read_text(encoding="utf-8")
    headers = [line for line in text.splitlines() if line[:2].isdigit() and " - " in line]
    assert path.name == "WhatsApp Chat with E2E Friend.txt"
    assert len(headers) == count == 6
    assert e2e.NON_LATIN_WORD in text and "wa-mcp-e2e-1" in text
    assert all(h.startswith(("13/01/2024, ", "14/01/2024, ")) for h in headers)
    assert Path(e2e.__file__).read_bytes().isascii()  # the non-Latin word is written with escapes


def test_free_port_is_usable() -> None:
    import socket

    port = e2e.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


# the report


def sample_report() -> e2e.Report:
    report = e2e.Report(
        "20261123-100000", datetime(2026, 11, 23, 10, 0, 0), {"Machine": "Test", "Config": "config.json"}
    )
    report.add(
        e2e.StepResult(
            "preflight",
            "Bridge status",
            e2e.PASS,
            0.4,
            ["own number: 1***0001", "token: wam_abcdefghijklmnop"],
            "",
            "10:00:00",
        )
    )
    report.add(
        e2e.StepResult(
            "search",
            "Search | with a pipe",
            e2e.FAIL,
            1.25,
            ["hits: 0"],
            "no hit for 15550100002@s.whatsapp.net\nsecond line | cell",
            "10:00:05",
        )
    )
    report.add(
        e2e.StepResult("docker", "Docker mode", e2e.SKIPPED, 0.0, [], "docker is not installed", "10:00:06")
    )
    report.finished = datetime(2026, 11, 23, 10, 5, 0)
    return report


def test_report_renders_counts_table_evidence_and_masks(tmp_path: Path) -> None:
    files = [e2e.FileInfo("login-qr.png", 2048, "0123456789abcdef", "2026-11-23 10:01:00")]
    text = sample_report().render(files)
    assert text.startswith("# WA MCP end-to-end report 20261123-100000")
    assert "Result: **FAILED** (1 passed, 1 failed, 1 skipped)" in text
    assert "| preflight | PASS | 0.4 |  |" in text
    assert "| search | FAIL | 1.2 | no hit for 1***0002@s.whatsapp.net second line \\| cell |" in text
    assert "| docker | SKIPPED | 0.0 | docker is not installed |" in text
    assert "## search: Search | with a pipe" in text
    assert "- own number: 1***0001" in text and "- hits: 0" in text
    assert "wam_[hidden]" in text and "abcdefghijklmnop" not in text
    assert "15550100002" not in text
    assert "| `login-qr.png` | 2048 | 0123456789abcdef | 2026-11-23 10:01:00 |" in text
    assert "- Finished: 2026-11-23T10:05:00" in text


def test_report_verdicts() -> None:
    report = e2e.Report("r", datetime(2026, 1, 1), {})
    assert report.verdict() == "NOTHING WAS CHECKED"
    report.add(e2e.StepResult("a", "A", e2e.PASS, 0))
    assert report.verdict() == "PASSED"
    report.add(e2e.StepResult("b", "B", e2e.SKIPPED, 0))
    assert report.verdict() == "PASSED with skipped steps"
    report.add(e2e.StepResult("c", "C", e2e.FAIL, 0))
    assert report.verdict() == "FAILED"
    assert "No screenshots or other files were dropped" in report.render()
    assert "not finished" in report.render()


def test_report_is_written_and_lists_dropped_files(tmp_path: Path) -> None:
    shots = tmp_path / "run" / "screenshots"
    shots.mkdir(parents=True)
    (shots / "login-qr.png").write_bytes(b"not really a png")
    files = e2e.list_files(shots)
    assert [f.name for f in files] == ["login-qr.png"] and files[0].size == 16 and len(files[0].sha256) == 16
    target = sample_report().write(tmp_path / "run" / "report.md", files)
    body = target.read_text(encoding="utf-8")
    assert target.is_file() and "login-qr.png" in body
    assert e2e.list_files(tmp_path / "missing") == []


# plan, dry run and step selection


def test_step_selection() -> None:
    assert [s.id for s in e2e.select_steps("", "")][:3] == ["preflight", "login", "chats"]
    assert [s.id for s in e2e.select_steps("chats,search", "search")] == ["chats"]
    with pytest.raises(ValueError, match="unknown step ids: nope"):
        e2e.select_steps("nope", "")
    assert len({s.id for s in e2e.STEPS}) == len(e2e.STEPS)


def test_dry_run_prints_the_plan_and_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert e2e.main(["--dry-run", "--config", str(config_file), "--group", "Test group"]) == 0
    out = capsys.readouterr().out
    assert "plan" in out and "Nothing is contacted" in out
    assert out.count("\n 1. preflight") == 1 and "18. accounts" in out
    assert "[V--] Reply with quote and mention in the test group" in out
    assert sorted(p.name for p in tmp_path.rglob("*")) == before  # no run folder, no report
    assert e2e.main(["--dry-run", "--config", str(config_file), "--only", "nope"]) == 2
    assert e2e.main(["--list", "--config", str(config_file)]) == 0
    assert "send_self" in capsys.readouterr().out


def test_dry_run_through_the_command_line(tmp_path: Path) -> None:
    script = Path(e2e.__file__)
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(script), "--dry-run", "--config", str(config_file), "--group", "Caf\u00e9"],
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (
        done.returncode == 0 and "Caf\u00e9" in done.stdout and "WA MCP end-to-end check: plan" in done.stdout
    )
    assert not (tmp_path / "exports").exists() and not (tmp_path / "store").exists()


# steps with a fake bridge: what is sent and to whom


def test_send_self_checks_timing_and_duplicate_and_sends_only_to_the_own_number(
    tmp_path: Path, clock: list[float]
) -> None:
    bridge = FakeBridge(clock, waited_ms=9000)
    ctx = make_ctx(tmp_path, bridge, "--self-number", OWN, answers=["yes"])
    ctx.state["sending_enabled"] = True
    e2e.step_send_self(ctx)
    assert {s["to"] for s in bridge.sent} == {OWN}
    assert [s["human"] for s in bridge.sent] == [True, False]  # the repeat was refused and not recorded
    assert any("refused with status 409" in item for item in ctx.evidence)
    assert ctx.state["self_msg"]["id"] == "ID1"


def test_send_self_fails_when_the_wait_is_too_short_or_a_repeat_is_sent(
    tmp_path: Path, clock: list[float]
) -> None:
    quick = FakeBridge(clock, waited_ms=100)
    ctx = make_ctx(tmp_path, quick, "--self-number", OWN, answers=["yes"])
    ctx.state["sending_enabled"] = True
    with pytest.raises(e2e.Fail, match="outside the expected"):
        e2e.step_send_self(ctx)
    lax = FakeBridge(clock, waited_ms=9000)
    lax.refuse_duplicates = False
    ctx = make_ctx(tmp_path, lax, "--self-number", OWN, answers=["yes"])
    ctx.state["sending_enabled"] = True
    with pytest.raises(e2e.Fail, match="twice"):
        e2e.step_send_self(ctx)


def test_a_declined_confirmation_sends_nothing(tmp_path: Path, clock: list[float]) -> None:
    bridge = FakeBridge(clock)
    for step, argv in (
        (e2e.step_send_self, ("--self-number", OWN)),
        (e2e.step_reply_group, ("--self-number", OWN, "--group", GROUP)),
    ):
        ctx = make_ctx(tmp_path, bridge, *argv, answers=["no"])
        ctx.state["sending_enabled"] = True
        with pytest.raises(e2e.Skip, match="not confirmed"):
            step(ctx)
    assert bridge.sent == []


def test_group_steps_need_an_explicit_group_and_the_word_send(tmp_path: Path, clock: list[float]) -> None:
    bridge = FakeBridge(clock)
    ctx = make_ctx(tmp_path, bridge, "--self-number", OWN, answers=["send"])
    ctx.state["sending_enabled"] = True
    with pytest.raises(e2e.Skip, match="no test group named"):
        e2e.step_reply_group(ctx)
    ctx = make_ctx(tmp_path, bridge, "--self-number", OWN, "--group", GROUP, answers=["send"])
    ctx.state["sending_enabled"] = True
    e2e.step_reply_group(ctx)
    assert {s["to"] for s in bridge.sent} == {GROUP}
    assert bridge.sent[1]["reply_to_id"] == "ID1" and bridge.sent[1]["mentioned"] == [f"{OWN}@s.whatsapp.net"]


def test_sending_off_skips_every_send_step(tmp_path: Path, clock: list[float]) -> None:
    bridge = FakeBridge(clock)
    for step in (e2e.step_send_self, e2e.step_reply_group, e2e.step_schedule, e2e.step_webhook):
        ctx = make_ctx(tmp_path, bridge, "--self-number", OWN, "--group", GROUP)
        ctx.state["sending_enabled"] = False
        with pytest.raises(e2e.Skip, match="sending is off"):
            step(ctx)
    assert bridge.sent == []


def test_run_step_turns_exceptions_into_results(tmp_path: Path, clock: list[float]) -> None:
    ctx = make_ctx(tmp_path, FakeBridge(clock))

    def passes(c: e2e.Ctx) -> None:
        c.note("key", "value")

    def skips(c: e2e.Ctx) -> None:
        raise e2e.Skip("not here")

    def fails(c: e2e.Ctx) -> None:
        c.note("before", "the failure")
        raise e2e.Fail("wrong")

    def bridge_down(c: e2e.Ctx) -> None:
        raise BridgeError(0, "bridge not reachable at 127.0.0.1:8080")

    def odd(c: e2e.Ctx) -> None:
        raise KeyError("msg_id")

    results = [
        e2e.run_step(e2e.StepDef(n, n, f, "", ""), ctx)
        for n, f in (("a", passes), ("b", skips), ("c", fails), ("d", bridge_down), ("e", odd))
    ]
    assert [r.status for r in results] == [e2e.PASS, e2e.SKIPPED, e2e.FAIL, e2e.FAIL, e2e.FAIL]
    assert results[0].evidence == ["key: value"] and results[2].evidence == ["before: the failure"]
    assert "not reachable" in results[3].note and "KeyError" in results[4].note


def test_input_needs_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    args = e2e.parser().parse_args([])
    ctx = e2e.Ctx(
        args=args, cfg=make_cfg(tmp_path), run_id="r", run_dir=tmp_path, bridge=cast(Any, FakeBridge(clock))
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(e2e.NoInput):
        ctx.ask("question?")


# the encryption step on a synthetic store


def minimal_store(path: Path, sealed: bool) -> None:
    """Use the bridge schema, a passphrase and both stored field formats."""
    sealer = crypt.Sealer(crypt.derive_key(synth.PASSPHRASE, synth.SALT))
    conn = sqlite3.connect(path)
    for migration in synth.go_migrations():
        conn.executescript(migration)
    if sealed:
        conn.execute("INSERT INTO meta VALUES ('crypt_salt', ?)", (base64.b64encode(synth.SALT).decode(),))
        conn.execute("INSERT INTO meta VALUES ('crypt_check', ?)", (synth.GO_CHECK,))
    for i, text in enumerate(("hello Alice", "see you at the book club", "invoice attached")):
        aad = crypt.aad("chat_messages", "body", OWN, f"M{i}") if i else None
        body = sealer.seal_string(text, aad) if sealed else text
        conn.execute(
            "INSERT INTO chat_messages (conv,msg_id,body,author,sent_at) VALUES (?, ?, ?, ?, 1)",
            (OWN, f"M{i}", body, OWN),
        )
    if sealed:
        key = sealer.seal(b"fixture", crypt.aad("chat_messages", "media_key", OWN, "M0"))
        conn.execute("UPDATE chat_messages SET media_key = ? WHERE msg_id = 'M0'", (key.encode(),))
    conn.commit()
    conn.close()


def encrypted_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sealed: bool = True, **enc: Any
) -> wa_config.Config:
    store = tmp_path / "store"
    store.mkdir()
    minimal_store(store / "wa.db", sealed)
    monkeypatch.setenv("WA_MCP_PASSPHRASE", synth.PASSPHRASE)
    return make_cfg(tmp_path, encryption={"mode": "passphrase", **enc})


def test_encryption_step_passes_on_a_sealed_store_and_refuses_another_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    ctx = make_ctx(tmp_path, FakeBridge(clock))
    ctx.cfg = encrypted_cfg(tmp_path, monkeypatch)
    e2e.step_encryption(ctx)
    notes = "\n".join(ctx.evidence)
    assert "chat_messages.body sealed: 3 of 3" in notes and "refused: wrong passphrase" in notes
    assert "chat_messages.media_key sealed: 1 of 1" in notes
    assert "temporary copy: deleted" in notes
    assert os.environ["WA_MCP_PASSPHRASE"] == synth.PASSPHRASE  # restored


def test_encryption_step_fails_on_a_plain_store_and_skips_when_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    ctx = make_ctx(tmp_path, FakeBridge(clock))
    ctx.cfg = encrypted_cfg(tmp_path, monkeypatch, sealed=False)
    with pytest.raises(e2e.Fail, match="no encryption salt"):
        e2e.step_encryption(ctx)
    ctx.cfg = make_cfg(tmp_path, encryption={"mode": "off"})
    with pytest.raises(e2e.Skip, match=r"encryption.mode is off"):
        e2e.step_encryption(ctx)


def test_sampled_columns_match_bridge_vectors() -> None:
    data = json.loads((e2e.ROOT / "testdata" / "crypt_vectors_v2.json").read_text(encoding="utf-8"))
    assert {(v["table"], v["column"]) for v in data["vectors"]} == {
        (t, c) for t, columns in e2e.SEALED_COLUMNS.items() for c in columns
    }


def test_encryption_step_detects_plain_text_in_a_sealed_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    ctx = make_ctx(tmp_path, FakeBridge(clock))
    ctx.cfg = encrypted_cfg(tmp_path, monkeypatch)
    conn = sqlite3.connect(ctx.cfg.wa_db)
    conn.execute(
        "UPDATE chat_messages SET media_key = ? WHERE msg_id = 'M0'", (f"wa-mcp-e2e-{ctx.run_id} leaked",)
    )
    conn.commit()
    conn.close()
    with pytest.raises(e2e.Fail, match="plain form"):
        e2e.step_encryption(ctx)


# the real MCP server and the real HTTP server, in a temporary store


def real_cfg(tmp_path: Path) -> wa_config.Config:
    """A config with an empty store folder: enough for the server to start and answer bridge_status."""
    return wa_config.load(synth.make_config(tmp_path))


def test_stdio_client_talks_to_the_real_server(tmp_path: Path) -> None:
    cfg = real_cfg(tmp_path)
    client = e2e.McpStdio(cfg.file)
    client.start()
    try:
        assert "list_messages" in client.tool_names()
        status = client.call("bridge_status", {})
        assert status["ok"] is True and status["database"] is False
        assert client.call("list_accounts", {})["count"] == 1
        assert client.call("list_messages", {"limit": 1})["ok"] is False  # no database in this store
    finally:
        client.close()


def test_http_mcp_step_against_the_real_server(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeBridge([0.0]), answers=["yes"])
    ctx.cfg = real_cfg(tmp_path)
    started = time.monotonic()
    e2e.step_http_mcp(ctx)
    notes = "\n".join(ctx.evidence)
    assert "without a token: 401" in notes and "with a wrong token: 401" in notes
    assert "initialize with the token: 200" in notes and "after revoking the token: 401" in notes
    assert "wam_" not in notes
    assert time.monotonic() - started < 120
