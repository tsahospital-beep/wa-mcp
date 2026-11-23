"""Regression tests for the review findings on the SDK."""

from __future__ import annotations

import base64
import runpy
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import synth

import wa_mcp
from wa_mcp import config, crypt, db, files, history, links, people, transcribe
from wa_mcp.transcribe.base import Backend, Result

REPO = Path(__file__).resolve().parents[3]
PUBLIC = ["93.184.216.34"]
OUR = "15550100001:7@s.whatsapp.net"


def _sql(path: Path, *statements: tuple[str, tuple[Any, ...]]) -> None:
    conn = sqlite3.connect(path)
    for stmt, args in statements:
        conn.execute(stmt, args)
    conn.commit()
    conn.close()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SilentBridge:
    """History requests are accepted but the phone never sends anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request_history(self, chat: str, count: int = 50, before_id: str | None = None) -> dict[str, Any]:
        self.calls.append(chat)
        return {"ok": True}


# finding 1: "wae1:" marker


def test_marker_text_is_plain_without_salt(store: db.Store, cfg_path: Path) -> None:
    wa = cfg_path.parent / "store" / "wa.db"
    _sql(wa, ("UPDATE chat_messages SET body = ? WHERE msg_id = 'B1'", ("wae1:just text from Bob",)))
    assert db.message(synth.BOB, "B1")["body"] == "wae1:just text from Bob"
    assert [m["msg_id"] for m in db.messages(query="WAE1:JUST")] == ["B1"]


def test_damaged_sealed_field_returns_raw_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealer = crypt.Sealer(crypt.derive_key(synth.PASSPHRASE, synth.SALT))
    path = synth.make_config(tmp_path, encryption={"enabled": True, "passphrase_env": "TEST_WA_PASS"})
    wa = tmp_path / "store" / "wa.db"
    synth.build_wa_db(wa, sealer=sealer)
    _sql(wa, ("UPDATE chat_messages SET body = 'wae1:AAAAbroken' WHERE msg_id = 'A1'", ()))
    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    db.connect(config.load(path))
    rows = {m["msg_id"]: m for m in db.messages(conv=synth.ALICE)}
    assert rows["A1"]["body"] == "wae1:AAAAbroken"
    assert rows["A2"]["body"].startswith("Crème")


def test_put_derived_rechecks_salt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = synth.make_config(tmp_path)
    wa = tmp_path / "store" / "wa.db"
    synth.build_wa_db(wa)
    store = db.connect(config.load(plain))
    assert store.messages(limit=1)
    salt = base64.b64encode(synth.SALT).decode()
    _sql(
        wa,
        ("INSERT INTO meta (key, value) VALUES ('crypt_salt', ?)", (salt,)),
        ("INSERT INTO meta (key, value) VALUES ('crypt_check', ?)", (synth.GO_CHECK,)),
    )
    with pytest.raises(db.StoreError, match="encrypted"):
        store.put_derived(synth.ALICE, "A1", "document", "must not be stored in the clear")
    conn = sqlite3.connect(wa)
    assert conn.execute("SELECT COUNT(*) FROM derived_text WHERE msg_id = 'A1'").fetchone()[0] == 0
    conn.close()

    synth.make_config(tmp_path, encryption={"enabled": True, "passphrase_env": "TEST_WA_PASS"})
    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    store = db.connect(config.load(plain))
    store.put_derived(synth.ALICE, "A1", "document", "sealed now")
    conn = sqlite3.connect(wa)
    raw = conn.execute("SELECT content FROM derived_text WHERE msg_id = 'A1'").fetchone()[0]
    conn.close()
    assert raw.startswith("wae2:") and store.derived(synth.ALICE, "A1")[0]["content"] == "sealed now"


# schema additions from the bridge (tombstones, edits)


def test_deleted_messages_hidden_by_default(store: db.Store, cfg_path: Path) -> None:
    wa = cfg_path.parent / "store" / "wa.db"
    conn = sqlite3.connect(wa)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    for col in ("deleted_at", "edited_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {col} INTEGER")
    conn.execute("UPDATE chat_messages SET deleted_at = ? WHERE msg_id = 'A2'", (synth.T0 + 250,))
    conn.execute("UPDATE chat_messages SET edited_at = ? WHERE msg_id = 'A1'", (synth.T0 + 150,))
    conn.commit()
    conn.close()
    s = db.connect(config.load(cfg_path))
    assert "A2" not in [m["msg_id"] for m in s.messages(conv=synth.ALICE)]
    shown = {m["msg_id"]: m for m in s.messages(conv=synth.ALICE, include_deleted=True)}
    assert shown["A2"]["deleted_at"] == synth.T0 + 250 and shown["A1"]["edited_at"] == synth.T0 + 150
    ctx = s.context(synth.ALICE, "A3", before=1)
    assert ctx is not None and [m["msg_id"] for m in ctx["before"]] == ["A1"]
    assert s.message(synth.ALICE, "A2") is not None


def test_derived_stats_counts_without_text(store: db.Store) -> None:
    db.put_derived(synth.ALICE, "A4", "document", "Plan text")
    stats = db.derived_stats([synth.ALICE, synth.ALICE_LID])
    assert [(r["source"], r["count"]) for r in stats] == [("document", 1), ("transcript", 1)]
    assert all(r["newest"] > 0 and "content" not in r for r in stats)
    assert db.derived_stats([]) == []


THUMBS = "\U0001f44d"
HEART = "\u2764\ufe0f"


def test_reactions_many(store: db.Store, cfg_path: Path) -> None:
    _sql(
        cfg_path.parent / "store" / "wa.db",
        (
            "INSERT INTO reactions (conv, msg_id, reactor, emoji, reacted_at) VALUES (?, 'A2', ?, ?, ?)",
            (synth.ALICE, synth.ALICE, THUMBS, synth.T0 + 210),
        ),
        (
            "INSERT INTO reactions (conv, msg_id, reactor, emoji, reacted_at) VALUES (?, 'A2', ?, ?, ?)",
            (synth.ALICE, synth.BOB, HEART, synth.T0 + 220),
        ),
    )
    got = db.reactions_many([(synth.ALICE, "A2"), (synth.ALICE, "A1")])
    assert [(r["reactor"], r["emoji"]) for r in got[(synth.ALICE, "A2")]] == [
        (synth.ALICE, THUMBS),
        (synth.BOB, HEART),
    ]
    assert (synth.ALICE, "A1") not in got


def test_reactions_sealed_and_missing_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealer = crypt.Sealer(crypt.derive_key(synth.PASSPHRASE, synth.SALT))
    path = synth.make_config(tmp_path, encryption={"enabled": True, "passphrase_env": "TEST_WA_PASS"})
    wa = tmp_path / "store" / "wa.db"
    synth.build_wa_db(wa, sealer=sealer)
    _sql(
        wa,
        (
            "INSERT INTO reactions (conv, msg_id, reactor, emoji, reacted_at) VALUES (?, 'A2', ?, ?, ?)",
            (synth.ALICE, synth.BOB, sealer.seal_string(THUMBS), synth.T0 + 210),
        ),
    )
    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    s = db.connect(config.load(path))
    assert s.reactions_many([(synth.ALICE, "A2")])[(synth.ALICE, "A2")][0]["emoji"] == THUMBS
    _sql(wa, ("DROP TABLE reactions", ()))
    assert db.connect(config.load(path)).reactions_many([(synth.ALICE, "A2")]) == {}


# finding 5: send targets


def test_send_target_never_uses_push_name(store: db.Store, cfg_path: Path) -> None:
    assert people.resolve_recipient("Alice Example") == [synth.ALICE, synth.ALICE_LID]
    assert people.resolve_recipient("alice") == [synth.ALICE, synth.ALICE_LID]
    assert people.resolve_recipient("Bob the Builder") == [synth.BOB]
    assert people.resolve_recipient("+1 555 0100 003") == [synth.BOB]
    assert people.resolve_recipient(synth.GROUP) == [synth.GROUP]
    for push_or_title in ("Ally", "Bobby", "Carol", "Book club", "Alice Ex"):
        with pytest.raises(LookupError):
            people.resolve_recipient(push_or_title)
    _sql(
        cfg_path.parent / "store" / "session.db",
        (
            "INSERT INTO whatsmeow_contacts (our_jid, their_jid, full_name) VALUES (?, ?, ?)",
            (OUR, "15550100009@s.whatsapp.net", "Alice Example"),
        ),
    )
    synth.sync_identities(cfg_path.parent / "store")
    people.connect(store=store)
    with pytest.raises(people.AmbiguousChat) as err:
        people.resolve_recipient("Alice Example")
    assert {c["jid"] for c in err.value.candidates} == {synth.ALICE, "15550100009@s.whatsapp.net"}
    assert people.name_info(synth.CAROL_LID) == ("Carol", "push")
    assert people.name_info(synth.ALICE)[1] == "contact"


# finding 12: session cache and the WAL file


def test_people_cache_sees_wal_writes(
    store: db.Store, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The names live in wa.db (wa_contacts, copied by the bridge); its writes land in the WAL first.
    writer = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    writer.execute("PRAGMA journal_mode = WAL")
    people.connect(store=store)
    dave = "15550100008@s.whatsapp.net"
    assert people.name_of(dave) == "+15550100008"
    writer.execute("INSERT INTO wa_contacts (jid, full_name) VALUES (?, ?)", (dave, "Dave"))
    writer.commit()
    monkeypatch.setattr(people, "_RELOAD_SECONDS", 0.0)
    try:
        assert people.name_of(dave) == "Dave"
    finally:
        writer.close()


# finding 17: XML member cap and running total


def test_xml_member_cap(tmp_path: Path) -> None:
    doc = tmp_path / "big.docx"
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("word/document.xml", "<w:document>" + "x" * 5000 + "</w:document>")
    with pytest.raises(files.FileError, match="larger than 1000 bytes"):
        files.extract_text(doc, files.Limits(max_xml_bytes=1000))
    assert files.Limits().max_xml_bytes == 20 * 1024 * 1024


def test_running_unpacked_total(tmp_path: Path) -> None:
    pack = tmp_path / "two.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("a.txt", "a" * 600)
        zf.writestr("b.txt", "b" * 600)
    with zipfile.ZipFile(pack) as zf:
        reader = files._Zip(zf, files.Limits(max_total_uncompressed=1000))
        reader.read(zf.getinfo("a.txt"), 10_000)
        with pytest.raises(files.FileError, match="more than 1000 bytes"):
            reader.read(zf.getinfo("b.txt"), 10_000)


# finding 20: install hints


def test_install_hints_use_the_workspace() -> None:
    hint = wa_mcp.install_hint("faster-whisper")
    assert "uv sync --all-packages --inexact --extra faster-whisper" in hint
    assert 'uv pip install -e "./sdk/python[faster-whisper]"' in hint
    assert wa_mcp.install_hint("encryption") == crypt.INSTALL_HINT
    assert wa_mcp.install_hint("files") == files.FILES_HINT
    for script in ("install_whisper.sh", "install_whisper.ps1"):
        text = (REPO / "scripts" / script).read_text(encoding="utf-8")
        assert "uv sync --all-packages" in text and "uv run python" in text
        assert 'install "wa-mcp-sdk' not in text and "pip install" not in text


# finding 30: time budgets


def test_sync_all_history_time_budget(store: db.Store) -> None:
    clock = FakeClock()
    res = history.sync_all_history(
        SilentBridge(),  # type: ignore[arg-type]
        "2020-01-01",
        "2020-01-01",
        wait_seconds=30,
        budget_seconds=45,
        sleep=clock.sleep,
        clock=clock,
    )
    assert res["complete"] is False and res["remaining"] >= 1
    assert "again" in res["resume"]
    assert clock.now <= 47


class OneSecond(Backend):
    name = "one-second"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def check(self) -> None:
        return

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        self.clock.now += 100
        return Result("words", "en", 0.9)


def test_transcribe_chat_time_budget(store: db.Store, cfg_path: Path, tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    from test_transcribe import tone

    _sql(
        cfg_path.parent / "store" / "wa.db",
        (
            "INSERT INTO chat_messages (conv, msg_id, author, sent_at, kind) VALUES (?, 'A9', ?, ?, 'voice')",
            (synth.ALICE, synth.ALICE, synth.T0 + 450),
        ),
    )
    clip = tone(tmp_path / "v.wav", 1.0)

    class Media:
        def download_media(self, chat: str, msg_id: str) -> dict[str, Any]:
            return {"path": str(clip)}

    clock = FakeClock()
    res = transcribe.transcribe_chat(
        Media(),  # type: ignore[arg-type]
        synth.ALICE,
        force=True,
        backend=OneSecond(clock),
        budget_seconds=50,
        clock=clock,
    )
    assert len(res["transcribed"]) == 1
    assert res["complete"] is False and res["remaining"] == 1 and "force=false" in res["resume"]


# finding 40/41: connect to the checked address


def test_fetch_connects_to_checked_address() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    got = links.guarded_get(
        "https://docs.google.com/document/d/x/export?format=txt",
        allowed_hosts=links.DOC_HOSTS,
        transport=httpx.MockTransport(handler),
        resolve=lambda host: PUBLIC,
    )
    req = seen[0]
    assert req.url.host == PUBLIC[0] and req.url.path == "/document/d/x/export"
    assert req.url.params["format"] == "txt"
    assert req.headers["host"] == "docs.google.com"
    assert req.extensions["sni_hostname"] == "docs.google.com"
    assert got.url == "https://docs.google.com/document/d/x/export?format=txt"
    assert "IP address" in (links.__doc__ or "")


# finding 42: done only when the date is reached


def test_history_exhausted_after_empty_pages(store: db.Store) -> None:
    """The phone sends nothing older: after EXHAUST_AFTER empty requests the chat is exhausted, the
    next call skips it and sync_all_history completes."""
    clock = FakeClock()
    bridge = SilentBridge()
    kwargs: dict[str, Any] = {"wait_seconds": 1, "sleep": clock.sleep, "clock": clock}
    first = history.sync_all_history(bridge, "2020-01-01", "2020-01-01", **kwargs)  # type: ignore[arg-type]
    assert first["complete"] is True and "resume" not in first
    assert {c["state"] for c in first["chats"]} == {"exhausted"}
    assert all(c["pages"] == history.EXHAUST_AFTER for c in first["chats"])
    calls = len(bridge.calls)
    second = history.sync_all_history(bridge, "2020-01-01", "2020-01-01", **kwargs)  # type: ignore[arg-type]
    assert second["complete"] is True and second["chats"] == [] and len(bridge.calls) == calls
    assert second["skipped_done"] == first["total_active"]


def test_history_empty_pages_carry_over_between_calls(store: db.Store) -> None:
    clock = FakeClock()
    bridge = SilentBridge()
    kwargs: dict[str, Any] = {
        "wait_seconds": 1,
        "sleep": clock.sleep,
        "clock": clock,
        "max_pages_per_chat": 2,
    }
    first = history.sync_all_history(bridge, "2020-01-01", "2020-01-01", max_chats=1, **kwargs)  # type: ignore[arg-type]
    assert first["chats"][0]["state"] == "pending" and first["chats"][0]["empty_pages"] == 2
    again = history.sync_all_history(bridge, "2020-01-01", "2020-01-01", max_chats=1, **kwargs)  # type: ignore[arg-type]
    assert (
        again["chats"][0]["chat"] == first["chats"][0]["chat"] and again["chats"][0]["state"] == "exhausted"
    )
    reached = history.sync_all_history(bridge, "2020-01-01", "2025-12-30", **kwargs)  # type: ignore[arg-type]
    assert reached["complete"] is True


# finding 53: one phone rule with the bridge


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+1 555 0100 002", "15550100002@s.whatsapp.net"),
        ("1555010", "1555010@s.whatsapp.net"),
        ("155501", None),
        ("05550100002", None),
        ("1" * 16, None),
        ("Alice", None),
    ],
)
def test_phone_rule_matches_bridge(text: str, expected: str | None) -> None:
    assert people.phone_jid(text) == expected


# finding 54: no dead login.language key


def test_login_language_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "language" not in config.DEFAULTS["login"]
    script = REPO / "scripts" / "check_config_example.py"
    assert "login.language" not in script.read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(script)])
    module = runpy.run_path(str(script))
    assert module["main"]() == 0
