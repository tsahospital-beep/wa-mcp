"""Synthetic bridge store for tests: wa.db built from the Go migrations, fake session.db.

All data is fictional (Alice, Bob, Carol, numbers in the 555-01xx range).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
STORE_GO = REPO / "bridge" / "internal" / "store" / "store.go"

# Columns of the whatsmeow tables people.py reads (whatsmeow store/sqlstore/upgrades/00-latest-schema.sql).
SESSION_SCHEMA = """
CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, lid TEXT, registration_id BIGINT NOT NULL DEFAULT 0);
CREATE TABLE whatsmeow_contacts (
  our_jid TEXT, their_jid TEXT, first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT,
  redacted_phone TEXT, PRIMARY KEY (our_jid, their_jid));
CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL);
"""

# Produced by bridge/internal/crypt (Go) for passphrase/salt below; see test_crypt.py.
PASSPHRASE = "correct horse battery"
SALT = b"0123456789abcdef"
GO_KEY_HEX = "aa8825620ede9c1bfdd5572d213586d84fd942fd54f69ff72b1355ee038c1bd1"
GO_SEALED = "wae1:y9a90//vxT3Nv8f1VSvqHwLYOPH1VacOQLNxO4t7bqMHCxE6mMJZ8GV+WiJGkw=="
GO_SEALED_PLAIN = "Café über αβγ"
GO_CHECK = "wae1:CY9buohgScy+i83KmBzwylz786CEd430Aki1SIC3Z8noGQ=="
GO_BLOB = "wae1:aTG24Vcf+jRyXtVqzug/soBhYAfDkda8XJ4hAElNozn3"
GO_BLOB_PLAIN = bytes([0, 1, 2, 250, 255])

ME = "15550100001@s.whatsapp.net"
ALICE = "15550100002@s.whatsapp.net"
ALICE_LID = "200000000000002@lid"
BOB = "15550100003@s.whatsapp.net"
CAROL_LID = "200000000000004@lid"
GROUP = "120363000000000001@g.us"
T0 = 1_767_000_000  # fixed base time


def go_migrations() -> list[str]:
    """The CREATE/ALTER statements of the bridge, read from store.go."""
    src = STORE_GO.read_text(encoding="utf-8")
    block = re.search(r"var migrations = \[\]string\{(.*?)\n\}", src, re.S)
    if block is None:
        raise AssertionError("migrations block not found in store.go")
    return re.findall(r"`([^`]*)`", block.group(1))


def build_wa_db(path: Path, sealer: Any = None) -> None:
    conn = sqlite3.connect(path)
    for i, sql in enumerate(go_migrations(), start=1):
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(i),),
        )
    conn.execute("PRAGMA journal_mode = WAL")

    def seal(text: str | None) -> str | None:
        if sealer is None or not text:
            return text
        return str(sealer.seal_string(text))

    if sealer is not None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('crypt_salt', ?)", (base64.b64encode(SALT).decode(),)
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('crypt_check', ?)", (GO_CHECK,))
    convs = [
        (ALICE, "Alice", 0, T0 + 500),
        (ALICE_LID, "", 0, T0 + 600),
        (BOB, "Bob", 0, T0 + 300),
        (GROUP, "Book club", 1, T0 + 400),
    ]
    conn.executemany("INSERT INTO conversations VALUES (?, ?, ?, ?)", convs)
    msgs: list[dict[str, Any]] = [
        {"conv": ALICE, "msg_id": "A1", "author": ALICE, "sent_at": T0 + 100, "body": "Hello from Alice"},
        {
            "conv": ALICE,
            "msg_id": "A2",
            "author": ME,
            "outgoing": 1,
            "sent_at": T0 + 200,
            "body": "Crème BRÛLÉE at seven",
            "reply_to_id": "A1",
            "reply_to_author": ALICE,
            "reply_preview": "Hello from Alice",
        },
        {
            "conv": ALICE,
            "msg_id": "A3",
            "author": ALICE,
            "sent_at": T0 + 300,
            "body": "",
            "kind": "voice",
            "mime": "audio/ogg; codecs=opus",
            "byte_size": 4000,
            "media_key": b"k" * 32,
        },
        {
            "conv": ALICE,
            "msg_id": "A4",
            "author": ALICE,
            "sent_at": T0 + 400,
            "body": "the plan",
            "kind": "document",
            "attachment_name": "plan.pdf",
            "mime": "application/pdf",
            "byte_size": 900,
        },
        {
            "conv": ALICE_LID,
            "msg_id": "L1",
            "author": ALICE_LID,
            "sent_at": T0 + 600,
            "body": "Καλημέρα ΚΟΣΜΕ via lid",
        },
        {"conv": BOB, "msg_id": "B1", "author": BOB, "sent_at": T0 + 300, "body": "GROSSE STRASSE 5"},
        {
            "conv": GROUP,
            "msg_id": "G1",
            "author": CAROL_LID,
            "sent_at": T0 + 350,
            "body": "Next book: see https://docs.google.com/document/d/AbCdEf123_-x/edit please",
            "mentioned": json.dumps([ALICE]),
        },
        {
            "conv": GROUP,
            "msg_id": "G2",
            "author": BOB,
            "sent_at": T0 + 400,
            "body": "",
            "kind": "sticker",
            "mime": "image/webp",
            "local_path": None,
        },
    ]
    for m in msgs:
        conn.execute(
            "INSERT INTO chat_messages (conv, msg_id, author, outgoing, sent_at, body, kind, attachment_name, mime, "
            "byte_size, media_key, local_path, reply_to_id, reply_to_author, reply_preview, mentioned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m["conv"],
                m["msg_id"],
                m["author"],
                m.get("outgoing", 0),
                m["sent_at"],
                seal(m["body"]) or "",
                m.get("kind", "text"),
                seal(m.get("attachment_name")),
                m.get("mime"),
                m.get("byte_size"),
                m.get("media_key"),
                m.get("local_path"),
                m.get("reply_to_id"),
                m.get("reply_to_author"),
                seal(m.get("reply_preview")),
                m.get("mentioned"),
            ),
        )
    conn.execute(
        "INSERT INTO derived_text VALUES (?, ?, 'transcript', 'en', 0.93, ?, ?)",
        (ALICE, "A3", seal("Bring the éclair recipe"), T0 + 310),
    )
    conn.execute(
        "INSERT INTO call_log VALUES ('C1', ?, NULL, 0, 1, ?, ?, 'answered')", (BOB, T0 + 50, T0 + 110)
    )
    conn.execute("INSERT INTO call_log VALUES ('C2', ?, NULL, 0, 0, ?, NULL, 'missed')", (ALICE, T0 + 70))
    conn.execute(
        "INSERT INTO scheduled_messages (target, body, next_run, recurrence, status, created_at, mentions) "
        "VALUES (?, ?, ?, 'weekly', 'pending', ?, ?)",
        (GROUP, seal("Reading tonight"), T0 + 9000, T0, json.dumps([BOB])),
    )
    conn.commit()
    conn.close()
    sync_identities(path.parent, sealer)


def sync_identities(store: Path, sealer: Any = None) -> None:
    """What the bridge does at start: copy names, the LID map (as full JIDs) and the own addresses from
    session.db into wa.db (wa_contacts, wa_lid_map, meta own_jid/own_lid). Call it again after a test
    changes session.db."""
    wa, sess = store / "wa.db", store / "session.db"
    if not (wa.is_file() and sess.is_file()):
        return
    from wa_mcp.crypt import aad

    w = sqlite3.connect(wa)
    if not w.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wa_contacts'").fetchone():
        w.close()
        return
    s = sqlite3.connect(sess)
    cols = ("first_name", "full_name", "push_name", "business_name")
    for their, *names in s.execute(f"SELECT their_jid, {', '.join(cols)} FROM whatsmeow_contacts"):
        if not their:
            continue
        values = [
            str(sealer.seal_string(v, aad("wa_contacts", c, their)))
            if sealer is not None and v
            else (v or "")
            for c, v in zip(cols, names, strict=True)
        ]
        w.execute(
            "INSERT INTO wa_contacts (jid, first_name, full_name, push_name, business_name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (jid) DO UPDATE SET first_name = excluded.first_name, "
            "full_name = excluded.full_name, push_name = excluded.push_name, "
            "business_name = excluded.business_name",
            (their, *values, T0),
        )
    for lid, pn in s.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
        w.execute(
            "INSERT INTO wa_lid_map (lid, pn, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (lid) DO UPDATE SET pn = excluded.pn",
            (f"{lid}@lid", f"{pn}@s.whatsapp.net", T0),
        )
    w.execute("DELETE FROM meta WHERE key IN ('own_jid', 'own_lid')")
    for jid, lid in s.execute("SELECT jid, lid FROM whatsmeow_device"):
        for key, value, server in (("own_jid", jid, "s.whatsapp.net"), ("own_lid", lid, "lid")):
            if value:
                user = value.split("@", 1)[0].split(":", 1)[0].split(".", 1)[0]
                w.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, f"{user}@{server}"))
    s.close()
    w.commit()
    w.close()


def build_session_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SESSION_SCHEMA)
    conn.execute(
        "INSERT INTO whatsmeow_device (jid, lid) VALUES (?, ?)",
        ("15550100001:7@s.whatsapp.net", "200000000000001:7@lid"),
    )
    our = "15550100001:7@s.whatsapp.net"
    conn.executemany(
        "INSERT INTO whatsmeow_contacts (our_jid, their_jid, first_name, full_name, push_name, business_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (our, ALICE, "Alice", "Alice Example", "Ally", None),
            (our, BOB, None, None, "Bobby", None),
            (our, CAROL_LID, None, None, "Carol", None),
        ],
    )
    conn.executemany(
        "INSERT INTO whatsmeow_lid_map VALUES (?, ?)",
        [("200000000000002", "15550100002"), ("200000000000001", "15550100001")],
    )
    conn.commit()
    conn.close()
    sync_identities(path.parent)


def make_config(root: Path, port: int = 0, **extra: Any) -> Path:
    """Write config.json + store under root and return the config path."""
    store = root / "store"
    store.mkdir(parents=True, exist_ok=True)
    cfg: dict[str, Any] = {"api": {"listen": f"127.0.0.1:{port}", "token": "test-token-0123456789"}}
    for key, value in extra.items():
        cfg[key] = value
    path = root / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


# A second account for the accounts tests: our number 15550100009 and a chat with Dana.
WORK_ME = "15550100009@s.whatsapp.net"
DANA = "15550100005@s.whatsapp.net"


def build_work_store(store: Path) -> None:
    """wa.db and session.db of a second account: the usual data plus Dana and a different own number."""
    store.mkdir(parents=True, exist_ok=True)
    build_wa_db(store / "wa.db")
    build_session_db(store / "session.db")
    conn = sqlite3.connect(store / "wa.db")
    conn.execute("INSERT INTO conversations VALUES (?, 'Dana', 0, ?)", (DANA, T0 + 700))
    conn.execute(
        "INSERT INTO chat_messages (conv, msg_id, author, sent_at, body) VALUES (?, 'W1', ?, ?, ?)",
        (DANA, DANA, T0 + 700, "Can you send the quarterly report?"),
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(store / "session.db")
    conn.execute("UPDATE whatsmeow_device SET jid = ?, lid = NULL", ("15550100009:3@s.whatsapp.net",))
    conn.execute(
        "INSERT INTO whatsmeow_contacts (our_jid, their_jid, full_name) VALUES (?, ?, 'Dana Work')",
        (WORK_ME, DANA),
    )
    conn.commit()
    conn.close()
    sync_identities(store)


def make_private(path: Path) -> None:
    """Mode 0600, and on Windows an access list with the current user and SYSTEM only (as the bridge wants)."""
    path.chmod(0o600)
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/remove:g",
                "*S-1-5-32-544",
                "*S-1-3-4",
                "*S-1-5-11",
                "*S-1-5-32-545",
                "*S-1-1-0",
                "/grant:r",
                f"{user}:F",
                "SYSTEM:F",
            ],
            capture_output=True,
            check=True,
        )
