from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
import synth

from wa_mcp import config, crypt, db, people


def test_crypt_matches_go_vector() -> None:
    key = crypt.derive_key(synth.PASSPHRASE, synth.SALT)
    assert key.hex() == synth.GO_KEY_HEX
    s = crypt.Sealer(key)
    assert s.open_string(synth.GO_SEALED) == synth.GO_SEALED_PLAIN
    assert s.verify_check(synth.GO_CHECK)
    assert s.open_blob(synth.GO_BLOB.encode()) == synth.GO_BLOB_PLAIN
    again = s.seal_string("round trip")
    assert again.startswith("wae1:") and s.open_string(again) == "round trip"
    assert s.open_string("plain stays") == "plain stays"
    wrong = crypt.Sealer(crypt.derive_key("wrong", synth.SALT))
    assert not wrong.verify_check(synth.GO_CHECK)
    with pytest.raises(crypt.CryptError):
        wrong.open(synth.GO_SEALED)


def test_config_defaults_and_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    monkeypatch.delenv("WA_MCP_SENDING_ENABLED", raising=False)
    missing = config.load(tmp_path / "nope.json")
    assert missing.store_dir == tmp_path / "store"
    assert missing.api.listen == "127.0.0.1:8080"
    assert not missing.sending.enabled and missing.sending.require_confirm
    assert missing.sending.pause_between_messages_seconds == (1.0, 3.5)
    assert missing.transcription.backend == "faster-whisper"
    assert missing.encryption.passphrase_env == "WA_MCP_PASSPHRASE"

    sub = tmp_path / "conf"
    sub.mkdir()
    (sub / "config.json").write_text(
        json.dumps({"store_dir": "data", "sending": {"enabled": True, "allowed_media_dirs": ["media"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WA_MCP_CONFIG", str(sub / "config.json"))
    cfg = config.load()
    assert cfg.store_dir == sub / "data"
    assert cfg.wa_db == sub / "data" / "wa.db"
    assert cfg.sending.enabled
    assert cfg.sending.allowed_media_dirs == (sub / "media",)
    monkeypatch.setenv("WA_MCP_SENDING_ENABLED", "false")
    assert not config.load().sending.enabled
    monkeypatch.setenv("WA_MCP_SENDING_ENABLED", "maybe")
    with pytest.raises(config.ConfigError):
        config.load()


def test_schema_columns_used_by_python_exist(tmp_path: Path) -> None:
    path = tmp_path / "wa.db"
    synth.build_wa_db(path)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    used = {c.strip() for c in db._MESSAGE_COLUMNS.split(",")}
    assert used <= cols
    derived_cols = {r[1] for r in conn.execute("PRAGMA table_info(derived_text)")}
    assert {"conv", "msg_id", "source", "lang", "confidence", "content", "created_at"} <= derived_cols
    assert int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]) >= 2
    conn.close()


def _whatsmeow_schema() -> Path | None:
    """latest-schema SQL of the whatsmeow version in bridge/go.mod, from `go env GOMODCACHE`."""
    go = shutil.which("go")
    gomod = synth.REPO / "bridge" / "go.mod"
    version = re.search(r"go\.mau\.fi/whatsmeow (v\S+)", gomod.read_text(encoding="utf-8"))
    if go is None or version is None:
        return None
    proc = subprocess.run([go, "env", "GOMODCACHE"], capture_output=True, text=True, timeout=60, check=False)
    cache = proc.stdout.strip()
    if proc.returncode != 0 or not cache:
        return None
    sql = Path(cache) / "go.mau.fi" / f"whatsmeow@{version.group(1)}" / "store" / "sqlstore" / "upgrades"
    path = sql / "00-latest-schema.sql"
    return path if path.is_file() else None


def test_session_fixture_matches_whatsmeow_schema() -> None:
    path = _whatsmeow_schema()
    if path is None:
        if os.environ.get("WA_MCP_REQUIRE_GO"):
            pytest.fail("whatsmeow module not found via go env GOMODCACHE")
        pytest.skip("Go or the whatsmeow module cache is not available")
    found = [path]
    sql = found[-1].read_text(encoding="utf-8")
    for table, cols in (
        ("whatsmeow_contacts", ["their_jid", "first_name", "full_name", "push_name", "business_name"]),
        ("whatsmeow_lid_map", ["lid", "pn"]),
        ("whatsmeow_device", ["jid", "lid"]),
    ):
        body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
        assert body is not None
        for col in cols:
            assert re.search(rf"^\s*{col}\s", body.group(1), re.M), (table, col)


def test_conversations_and_messages(store: db.Store) -> None:
    chats = db.conversations()
    assert [c["jid"] for c in chats][:2] == [synth.ALICE_LID, synth.ALICE]
    assert db.conversations(query="book CLUB")[0]["jid"] == synth.GROUP
    assert db.conversation(synth.GROUP)["is_group"] is True
    msgs = db.messages(conv=synth.ALICE)
    assert [m["msg_id"] for m in msgs] == ["A4", "A3", "A2", "A1"]
    asc = db.messages(conv=[synth.ALICE, synth.ALICE_LID], ascending=True, limit=2, offset=3)
    assert [m["msg_id"] for m in asc] == ["A4", "L1"]
    m = db.message(synth.ALICE, "A2")
    assert m is not None and m["outgoing"] is True and m["reply_preview"] == "Hello from Alice"
    assert "media_key" not in m and m["time"]
    assert db.messages(after=synth.T0 + 350, before=synth.T0 + 600, limit=None)
    assert [x["msg_id"] for x in db.messages(kind=["voice", "sticker"])] == ["G2", "A3"]
    assert db.find_message("G1")["mentioned"] == [synth.ALICE]


@pytest.mark.parametrize(
    ("needle", "expected"),
    [
        ("crème brûlée", "A2"),  # accented Latin, upper in the text
        ("κοσμε", "L1"),  # Greek lower against Greek upper
        ("große straße", "B1"),  # casefold maps sharp s to ss
        ("ÉCLAIR", "A3"),  # found through the transcript
    ],
)
def test_casefold_search(store: db.Store, needle: str, expected: str) -> None:
    assert [m["msg_id"] for m in db.messages(query=needle)] == [expected]


def test_context_derived_calls_scheduled(store: db.Store) -> None:
    ctx = db.context(synth.ALICE, "A2", before=1, after=1)
    assert ctx is not None
    assert [m["msg_id"] for m in ctx["before"]] == ["A1"]
    assert [m["msg_id"] for m in ctx["after"]] == ["A3"]
    assert db.context(synth.ALICE, "missing") is None
    db.put_derived(synth.ALICE, "A4", "document", "Plan text", lang="en", confidence=None)
    db.put_derived(synth.ALICE, "A4", "document", "Plan text v2")
    rows = db.derived(synth.ALICE, "A4")
    assert len(rows) == 1 and rows[0]["content"] == "Plan text v2"
    many = db.derived_many([(synth.ALICE, "A3"), (synth.ALICE, "A4")])
    assert set(many) == {(synth.ALICE, "A3"), (synth.ALICE, "A4")}
    with pytest.raises(ValueError, match="source"):
        db.put_derived(synth.ALICE, "A4", "Bad Source", "x")
    calls = db.calls()
    assert [c["call_id"] for c in calls] == ["C2", "C1"]
    assert calls[1]["duration_seconds"] == 60 and calls[1]["video"] is True
    sched = db.scheduled(status="pending")
    assert sched[0]["body"] == "Reading tonight" and sched[0]["mentions"] == [synth.BOB]
    assert db.active_chats(after=synth.T0 + 350)[0]["jid"] == synth.ALICE_LID


def test_store_is_opened_read_only(store: db.Store) -> None:
    with store._ro() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM chat_messages")


def test_encrypted_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealer = crypt.Sealer(crypt.derive_key(synth.PASSPHRASE, synth.SALT))
    path = synth.make_config(tmp_path, encryption={"enabled": True, "passphrase_env": "TEST_WA_PASS"})
    synth.build_wa_db(tmp_path / "store" / "wa.db", sealer=sealer)
    conn = sqlite3.connect(tmp_path / "store" / "wa.db")
    raw = conn.execute("SELECT body FROM chat_messages WHERE msg_id = 'B1'").fetchone()[0]
    conn.execute("UPDATE chat_messages SET body = ? WHERE msg_id = 'A1'", (synth.GO_SEALED,))
    conn.commit()
    conn.close()
    assert raw.startswith("wae1:")

    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    s = db.connect(config.load(path))
    assert s.encrypted
    assert db.message(synth.ALICE, "A1")["body"] == synth.GO_SEALED_PLAIN
    assert [m["msg_id"] for m in db.messages(query="große")] == ["B1"]
    assert [m["msg_id"] for m in db.messages(query="éclair")] == ["A3"]
    assert [m["msg_id"] for m in db.messages(query="üBER")] == ["A1"]
    db.put_derived(synth.BOB, "B1", "document", "secret text")
    conn = sqlite3.connect(tmp_path / "store" / "wa.db")
    stored = conn.execute("SELECT content FROM derived_text WHERE msg_id = 'B1'").fetchone()[0]
    conn.close()
    # Bound to its place (wae2): the same value under another message does not open.
    assert stored.startswith("wae2:") and db.derived(synth.BOB, "B1")[0]["content"] == "secret text"

    monkeypatch.setenv("TEST_WA_PASS", "wrong")
    with pytest.raises(db.StoreError, match="wrong passphrase"):
        db.connect(config.load(path)).messages()
    plain = synth.make_config(tmp_path)
    with pytest.raises(db.StoreError, match="encrypted"):
        db.connect(config.load(plain)).messages()


def test_people(store: db.Store) -> None:
    assert people.same_person(synth.ALICE_LID) == [synth.ALICE, synth.ALICE_LID]
    assert people.same_person(synth.ALICE) == [synth.ALICE, synth.ALICE_LID]
    assert people.same_person("15550100002:3@s.whatsapp.net") == [synth.ALICE, synth.ALICE_LID]
    assert people.same_person(synth.GROUP) == [synth.GROUP]
    assert people.name_of(synth.ALICE_LID) == "Alice Example"
    assert people.name_of(synth.BOB) == "Bob the Builder"  # alias wins
    assert people.name_of(synth.CAROL_LID) == "Carol"
    assert people.name_of("15550100099@s.whatsapp.net") == "+15550100099"
    assert people.my_jids()[:2] == [synth.ME, "200000000000001@lid"]
    assert people.phone_of(synth.ALICE_LID) == "+15550100002"
    found = people.search("ally")
    assert found and found[0]["jids"] == [synth.ALICE, synth.ALICE_LID]
    assert people.search("0100003")[0]["name"] == "Bob the Builder"
    assert people.resolve_chat("+1 555 0100 002") == [synth.ALICE, synth.ALICE_LID]
    assert people.resolve_chat("book club") == [synth.GROUP]
    assert people.resolve_chat("Alice Example") == [synth.ALICE, synth.ALICE_LID]
    with pytest.raises(LookupError):
        people.resolve_chat("nobody at all")
