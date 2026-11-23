"""Round 3 review fixes on the Python side: fields bound to their place (wae2), names from wa.db instead of
session.db, the reader schema, the encryption state read per connection, passphrase files, imported
messages and task detection, image headers before OCR, and the AI provider transport limits."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import synth
from vision_data import png

from wa_mcp import config, crypt, db, people, secretfile, vision
from wa_mcp.ai import base as ai_base
from wa_mcp.ai.openai_compat import OpenAICompatible
from wa_mcp.insight import base as insight_base
from wa_mcp.insight import tasks

# Sealed by bridge/internal/crypt (Go) with synth.GO_KEY_HEX and this associated data.
GO_SEALED_V2 = "wae2:g0dSflvhMkSzjn7Dr/VNrniQNwOgYHxGdO1W7GUMzBaY+yKNO27YcJg="
GO_SEALED_V2_AAD = crypt.aad("wa_contacts", "full_name", synth.ALICE)


def _sealer() -> crypt.Sealer:
    return crypt.Sealer(bytes.fromhex(synth.GO_KEY_HEX))


def test_wae2_opens_only_in_its_place() -> None:
    s = _sealer()
    assert s.open_string(GO_SEALED_V2, GO_SEALED_V2_AAD) == "Alice Example"
    with pytest.raises(crypt.CryptError):
        s.open_string(GO_SEALED_V2, crypt.aad("wa_contacts", "full_name", synth.BOB))
    with pytest.raises(crypt.CryptError, match="needs its place"):
        s.open_string(GO_SEALED_V2)
    mine = s.seal_string("note", crypt.aad("derived_text", "content", synth.BOB, "B1"))
    assert mine.startswith("wae2:") and crypt.is_sealed(mine)
    assert s.open_string(synth.GO_SEALED) == synth.GO_SEALED_PLAIN  # wae1 values still open


def _encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    path = synth.make_config(tmp_path, encryption={"mode": "passphrase", "passphrase_env": "TEST_WA_PASS"})
    synth.build_wa_db(tmp_path / "store" / "wa.db", sealer=_sealer())
    synth.build_session_db(tmp_path / "store" / "session.db")
    return path


def test_names_come_from_wa_db_sealed_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _encrypted(tmp_path, monkeypatch)
    wa = tmp_path / "store" / "wa.db"
    conn = sqlite3.connect(wa)
    conn.execute("UPDATE wa_contacts SET full_name = ? WHERE jid = ?", (GO_SEALED_V2, synth.ALICE))
    conn.commit()
    conn.close()
    # The session file (keys of the linked device) is not needed any more.
    (tmp_path / "store" / "session.db").unlink()
    s = db.connect(config.load(path))
    people.connect(store=s)
    assert people.name_info(synth.ALICE) == ("Alice Example", "contact")
    assert people.name_info(synth.CAROL_LID) == ("Carol", "push")
    assert set(people.same_person(synth.ALICE)) == {synth.ALICE, synth.ALICE_LID}
    assert synth.ME in people.my_jids()
    # A value moved to another row does not open there: it is shown as stored, never as Bob's name.
    conn = sqlite3.connect(wa)
    conn.execute("UPDATE wa_contacts SET full_name = ? WHERE jid = ?", (GO_SEALED_V2, synth.BOB))
    conn.commit()
    conn.close()
    people.connect(store=s)
    assert people.name_of(synth.BOB) != "Alice Example"


def test_old_store_falls_back_to_session_with_a_warning(
    store: db.Store, cfg_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    conn.executescript(
        "DROP TABLE wa_contacts; DROP TABLE wa_lid_map; DELETE FROM meta WHERE key = 'min_reader_schema';"
    )
    conn.commit()
    conn.close()
    people.connect(store=store)
    with caplog.at_level(logging.WARNING, logger="wa_mcp.people"):
        assert people.name_of(synth.ALICE) == "Alice Example"
    assert any("deprecated" in r.getMessage() for r in caplog.records)


def test_newer_reader_schema_is_refused(store: db.Store, cfg_path: Path) -> None:
    conn = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'min_reader_schema'", (str(db.READER_SCHEMA + 1),))
    conn.commit()
    conn.close()
    with pytest.raises(db.StoreError, match="newer bridge"):
        db.messages(limit=1)


def test_encryption_turned_on_while_running(
    tmp_path: Path, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = config.load(cfg_path).raw
    path = synth.make_config(
        tmp_path, people=raw["people"], encryption={"mode": "passphrase", "passphrase_env": "TEST_WA_PASS"}
    )
    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    s = db.connect(config.load(path))
    assert s.message(synth.ALICE, "A1")["body"] and not s.encrypted
    # The bridge encrypts in place: salt, check and sealed values appear while this reader keeps running.
    sealer = _sealer()
    conn = sqlite3.connect(tmp_path / "store" / "wa.db")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('crypt_salt', ?)", (base64.b64encode(synth.SALT).decode(),)
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('crypt_check', ?)", (synth.GO_CHECK,))
    sealed = sealer.seal_string("now sealed", crypt.aad("chat_messages", "body", synth.ALICE, "A1"))
    conn.execute("UPDATE chat_messages SET body = ? WHERE msg_id = 'A1'", (sealed,))
    conn.commit()
    conn.close()
    assert s.message(synth.ALICE, "A1")["body"] == "now sealed"
    assert [m["msg_id"] for m in s.messages(query="NOW SEALED")] == ["A1"]


def test_passphrase_from_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _encrypted(tmp_path, monkeypatch)
    monkeypatch.delenv("TEST_WA_PASS")
    secret = tmp_path / "pass.txt"
    secret.write_text(synth.PASSPHRASE + "\n", encoding="utf-8")
    monkeypatch.setenv("TEST_WA_PASS_FILE", str(secret))
    # Like a key file: a passphrase file that others may read is refused, as the bridge does.
    if sys.platform != "win32":
        secret.chmod(0o644)
    with pytest.raises(db.StoreError, match="can be read by"):
        db.connect(config.load(path)).message(synth.BOB, "B1")
    synth.make_private(secret)
    s = db.connect(config.load(path))
    assert s.encrypted and s.message(synth.BOB, "B1")["body"]
    monkeypatch.delenv("TEST_WA_PASS_FILE")
    with pytest.raises(db.StoreError, match="TEST_WA_PASS_FILE"):
        db.connect(config.load(path)).message(synth.BOB, "B1")


def _imported_request(cfg_path: Path) -> None:
    conn = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    conn.execute(
        "INSERT INTO chat_messages (conv, msg_id, author, sent_at, body) VALUES (?, 'imp-0001', ?, ?, ?)",
        (synth.BOB, synth.BOB, synth.T0 - 30, "Please send me the old contract by Friday"),
    )
    conn.commit()
    conn.close()


def test_task_detection_skips_imported_messages(store: db.Store, cfg_path: Path) -> None:
    _imported_request(cfg_path)
    assert [m.id for m in insight_base.load([synth.BOB], imported=False) if m.id.startswith("imp-")] == []
    found = tasks.detect_tasks(synth.BOB, min_score=0, include_me=True)
    assert not [d for d in found["detections"] if "imp-0001" in str(d)]
    raw = config.load(cfg_path).raw
    raw["tasks"] = {**raw.get("tasks", {}), "include_imported": True}
    cfg_path.write_text(__import__("json").dumps(raw), encoding="utf-8")
    db.connect(config.load(cfg_path))
    found = tasks.detect_tasks(synth.BOB, min_score=0, include_me=True)
    assert [d for d in found["detections"] if "imp-0001" in str(d)]


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def _webp_vp8x(width: int, height: int) -> bytes:
    chunk = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little")
    chunk += (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


@pytest.mark.parametrize(
    ("data", "kind", "size"),
    [
        (png(640, 480), "image/png", (640, 480)),
        (_jpeg(1200, 900), "image/jpeg", (1200, 900)),
        (b"GIF89a" + struct.pack("<HH", 32, 16) + b"\x00" * 8, "image/gif", (32, 16)),
        (_webp_vp8x(300, 200), "image/webp", (300, 200)),
        (b"BM" + b"\x00" * 16 + struct.pack("<ii", 50, -40) + b"\x00" * 8, "image/bmp", (50, 40)),
    ],
)
def test_image_size_from_the_header(tmp_path: Path, data: bytes, kind: str, size: tuple[int, int]) -> None:
    p = tmp_path / "img"
    p.write_bytes(data)
    assert vision.image_size(p) == (kind, *size)


def test_huge_or_fake_images_are_refused_before_decoding(tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(png(20000, 20000))
    with pytest.raises(vision.OcrError, match="megapixels"):
        vision.check_image(bomb, 20 * 1024 * 1024)
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"MZ\x90\x00 not an image at all")
    with pytest.raises(vision.OcrError, match="not a JPEG"):
        vision.check_image(fake, 20 * 1024 * 1024)
    ok = tmp_path / "ok.png"
    ok.write_bytes(png(800, 600))
    vision.check_image(ok, 20 * 1024 * 1024)


def _provider(base_url: str, handler: Any) -> OpenAICompatible:
    cfg = config.AiConfig(
        provider="openai-compatible",
        base_url=base_url,
        text_model="m",
        vision_model="v",
        api_key_env="",
        timeout_s=5,
        max_calls_per_day=0,
        max_input_chars=1000,
    )
    return OpenAICompatible(cfg, transport=httpx.MockTransport(handler))


def test_ai_provider_needs_https_off_loopback() -> None:
    answer = {"choices": [{"message": {"content": "fine"}}]}
    with pytest.raises(ai_base.AIUnavailable, match="https"):
        _provider("http://ai.example.test/v1", lambda r: httpx.Response(200, json=answer)).complete("hi")
    assert (
        _provider("http://127.0.0.1:8000/v1", lambda r: httpx.Response(200, json=answer)).complete("hi")
        == "fine"
    )
    assert (
        _provider("https://ai.example.test/v1", lambda r: httpx.Response(200, json=answer)).complete("hi")
        == "fine"
    )


def test_ai_provider_answer_size_is_capped() -> None:
    huge = b'{"choices": [{"message": {"content": "' + b"a" * (ai_base.MAX_RESPONSE_BYTES + 10) + b'"}}]}'
    with pytest.raises(ai_base.AIError, match="more than"):
        _provider("https://ai.example.test/v1", lambda r: httpx.Response(200, content=huge)).complete("hi")


VECTORS_V2 = Path(__file__).resolve().parents[3] / "testdata" / "crypt_vectors_v2.json"


def _vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(VECTORS_V2.read_text(encoding="utf-8"))
    return data


def test_go_vectors_v2_open_here() -> None:
    """testdata/crypt_vectors_v2.json (written by the Go side) is the reference for the AAD format."""
    data = _vectors()
    s = crypt.Sealer(bytes.fromhex(data["key_hex"]))
    assert s.open_string(data["v1_sealed"]) == data["v1_plain"]
    for v in data["vectors"]:
        place = crypt.aad(v["table"], v["column"], v["conv"], v["msg_id"], v.get("source", ""))
        assert place == bytes.fromhex(v["aad_hex"]), v["table"]
        plain = bytes.fromhex(v["plain_hex"]) if "plain_hex" in v else v["plain"].encode()
        assert s.open(v["sealed"], place) == plain
        with pytest.raises(crypt.CryptError):
            s.open(v["sealed"], place + b"x")


def test_go_vectors_v2_through_the_store(
    store: db.Store, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sealed column the reader opens, with the values Go wrote for exactly those places."""
    data = _vectors()
    key = bytes.fromhex(data["key_hex"])
    by = {(v["table"], v["column"]): v for v in data["vectors"]}
    derived = by[("derived_text", "content")]
    assert derived.get("source"), "the Go vectors must bind derived_text to its source, as wa_mcp.db does"
    conn = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('crypt_salt', ?)", (base64.b64encode(synth.SALT).decode(),)
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('crypt_check', ?)",
        (crypt.Sealer(key).seal_string(crypt.CHECK_PLAINTEXT),),
    )
    body = by[("chat_messages", "body")]
    conn.execute(
        "UPDATE chat_messages SET body = ? WHERE conv = ? AND msg_id = ?",
        (body["sealed"], body["conv"], body["msg_id"]),
    )
    conn.execute(
        "INSERT OR REPLACE INTO derived_text (conv, msg_id, source, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (derived["conv"], derived["msg_id"], derived["source"], derived["sealed"], synth.T0),
    )
    sched = by[("scheduled_messages", "body")]
    conn.execute("UPDATE scheduled_messages SET body = ? WHERE target = ?", (sched["sealed"], sched["conv"]))
    contact = by[("wa_contacts", "full_name")]
    conn.execute("UPDATE wa_contacts SET full_name = ? WHERE jid = ?", (contact["sealed"], contact["conv"]))
    conn.commit()
    conn.close()
    raw = config.load(cfg_path).raw
    path = synth.make_config(
        cfg_path.parent, encryption={"mode": "keyfile", "key_file": "unused.key"}, people=raw["people"]
    )
    s = db.connect(config.load(path))
    monkeypatch.setattr(s, "_field_key", lambda kind, salt_text, salt: key)
    assert s.message(body["conv"], body["msg_id"])["body"] == body["plain"]
    assert [d["content"] for d in s.derived(derived["conv"], derived["msg_id"])] == [derived["plain"]]
    assert [r["body"] for r in s.scheduled() if r["target"] == sched["conv"]] == [sched["plain"]]
    ids = s.identities()
    assert ids is not None
    assert {c["jid"]: c["full_name"] for c in ids.contacts}[contact["conv"]] == contact["plain"]


def test_absolute_session_file(tmp_path: Path) -> None:
    """A session file outside the store (a volume only the bridge mounts) is taken as it is."""
    elsewhere = (tmp_path / "session" / "session.db").resolve()
    path = synth.make_config(tmp_path, session_file=str(elsewhere))
    assert config.load(path).session_db == elsewhere
    plain = synth.make_config(tmp_path / "b", session_file="phone.db")
    assert config.load(plain).session_db == (tmp_path / "b" / "store" / "phone.db")


@pytest.mark.parametrize(
    ("raw", "want"),
    [(b"pw\r\n", "pw"), (b"pw\n", "pw"), (b"pw\r", "pw\r"), (b" pw \n\n", " pw \n"), (b"pw", "pw")],
)
def test_passphrase_file_loses_one_line_break_only(tmp_path: Path, raw: bytes, want: str) -> None:
    """The same rule as the bridge (keystore.ReadPassphraseFile), so both derive the same key."""
    p = tmp_path / "pass"
    p.write_bytes(raw)
    synth.make_private(p)
    assert secretfile.read_passphrase(p) == want
