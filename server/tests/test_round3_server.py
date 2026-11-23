"""Round 3 review fixes in the server: imported messages are marked, and the untrusted note names the
text that is made from messages (transcripts, OCR, image descriptions, AI summaries)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth

from wa_mcp import config
from wa_mcp_server import app, common


def test_imported_messages_are_marked(tmp_path: Path) -> None:
    cfg = synth.make_config(tmp_path, port=1)
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    conn = sqlite3.connect(tmp_path / "store" / "wa.db")
    conn.execute(
        "INSERT INTO chat_messages (conv, msg_id, author, sent_at, body) VALUES (?, 'imp-0007', ?, ?, ?)",
        (synth.BOB, synth.BOB, synth.T0 - 60, "From the old export"),
    )
    conn.commit()
    conn.close()
    mcp = app.create_app(config.load(cfg))
    out = asyncio.run(mcp.call_tool("list_messages", {"chat": synth.BOB, "limit": 50}))
    blocks = out[0] if isinstance(out, tuple) else out
    data = json.loads("".join(getattr(b, "text", "") for b in blocks))
    by_id = {m["id"]: m for m in data["messages"]}
    assert by_id["imp-0007"]["imported"] is True
    assert "imported" not in by_id["B1"]


def test_untrusted_note_names_derived_text() -> None:
    for words in ("transcripts", "image text", "image descriptions", "ai_summary"):
        assert words in common.UNTRUSTED
