"""Regression tests for the second review round of the insight modules."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import insight_data as data
import pytest
import synth
from test_insight_tasks import open_world

from wa_mcp import db, fsutil
from wa_mcp.insight import analytics, base, mentions, pairing, state, style, tracker, when

THUMBS = chr(0x1F44D)
HEART = chr(0x2764)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[db.Store]:
    for name in ("WA_MCP_CONFIG", "WA_MCP_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield open_world(tmp_path)


# M7: absolute dates are capped, timestamp conversions never raise


def test_absolute_dates_are_capped(world: db.Store) -> None:
    ref = datetime(2026, 3, 16, 12, 0)
    assert when.parse_due("by 1990-01-01", ref) is None
    assert when.parse_due("by 3000-01-01", ref) is None
    assert when.parse_due("on 1/1/2090", ref) is None
    assert when.parse_due("by 2026-03-20", ref).day == date(2026, 3, 20)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="out of range"):
        when.day_bounds(date(9999, 12, 31))


def test_tracker_timestamps_never_raise() -> None:
    class Unrepresentable:
        def end(self) -> datetime:
            raise OSError("mktime out of range")

    assert tracker._due_ts(Unrepresentable()) is None  # type: ignore[arg-type]
    assert tracker._due_stamp(10**18) == "an unknown date"


# items 6/23: Feb 29


def test_feb_29_goes_to_the_next_leap_year(world: db.Store) -> None:
    common = datetime(2026, 11, 23, 12)
    assert when.parse_due("by Feb 29", common).day == date(2028, 2, 29)  # type: ignore[union-attr]
    assert when.parse_due("on 29/02", common).day == date(2028, 2, 29)  # type: ignore[union-attr]
    assert when.parse_due("by February 30", common) is None


# M8: anchored mail pattern, clip before masking


def test_mask_is_linear_on_long_text() -> None:
    for text in ("a" * 65_000, "a" * 32_000 + "@" + "b" * 32_000, ("x.y+z" * 13_000)):
        start = time.perf_counter()
        base.mask(text)
        assert time.perf_counter() - start < 0.2
    start = time.perf_counter()
    out = base.safe_text("call +1 555 0100 002 or mail alice.b+x@example.org " + "x" * 65_000, 60)
    assert time.perf_counter() - start < 0.2
    assert out is not None and "0100" not in out and "alice" not in out and len(out) <= 60
    assert base.mask("write to alice.b+x@example.org now") == "write to ***@example.org now"


# M13: fail closed, secure wipe, private file


def test_encryption_state_fails_closed(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: db.Store) -> bool:
        raise db.StoreError("cannot read wa.db")

    monkeypatch.setattr(db.Store, "encrypted", property(broken))
    assert state._encrypted() is True
    assert state.stored_text("copied message text") == ""


def test_wipe_leaves_no_text_in_the_file(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    restricted: list[Path] = []
    real_restrict = fsutil.restrict
    monkeypatch.setattr(fsutil, "restrict", lambda p: (restricted.append(p), real_restrict(p)))
    tracker.refresh(now=data.NOW_TS)
    assert world.cfg.insight_db in restricted
    conn = sqlite3.connect(world.path)
    bodies = [str(r[0]) for r in conn.execute("SELECT body FROM chat_messages WHERE length(body) > 15")]
    conn.close()
    path = world.cfg.insight_db
    titles = sqlite3.connect(path).execute("SELECT title FROM tasks WHERE title <> ''").fetchall()
    assert titles and any(t[0].encode() in path.read_bytes() for t in titles)
    monkeypatch.setattr(db.Store, "encrypted", property(lambda self: True))
    with state.connect() as conn:
        assert state.get_meta(conn, "text_mode") == "sealed"
    raw = path.read_bytes()
    for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal")):
        if side.exists():
            raw += side.read_bytes()
    assert not [b for b in bodies if b[:40].encode() in raw]


# 7/28: fixed reasons only; private report files with collision-free names


def test_reasons_use_fixed_wording_only(world: db.Store) -> None:
    tracker.refresh(now=data.NOW_TS)
    allowed = set(tracker.WHY.values())
    conn = sqlite3.connect(world.cfg.insight_db)
    reasons = [str(r[0]) for r in conn.execute("SELECT status_why FROM tasks")]
    reasons += [str(r[0]) for r in conn.execute("SELECT why FROM task_steps")]
    conn.close()
    assert reasons
    for reason in reasons:
        for part in reason.split("; "):
            core = re.sub(r" \([A-Z_]+\)$", "", part)
            assert core in allowed or re.fullmatch(r"no activity for more than \d+ days", core), reason


def test_reports_are_private_and_names_do_not_collide(
    world: db.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[Path] = []
    real_write = fsutil.write_private
    monkeypatch.setattr(fsutil, "write_private", lambda p, t: (written.append(p), real_write(p, t))[1])
    result: dict[str, Any] = {"meta": {"period": ["a", "b"], "messages": 0}, "summary": "s", "sections": {}}
    one = analytics.write_report(result, base.Target(("1@g.us",), "Αλφα", True))
    two = analytics.write_report(result, base.Target(("2@g.us",), "Βητα", True))
    assert one != two and one.exists() and two.exists()
    assert len(written) == 2 and all(p.parent == one.parent for p in written)
    assert re.fullmatch(r"analytics_chat_[0-9a-f]{8}\.md", one.name)


# 25: hedged, future and asked done words are not done


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("almost done", []),
        ("it will be ready tomorrow", []),
        ("done?", []),
        ("ready by Friday", []),
        ("it should be sent today", []),
        ("nearly finished", []),
        ("done, here it is", ["done", "here it is"]),
        ("sent it to you", ["sent"]),
    ],
)
def test_not_yet_done(world: db.Store, text: str, expected: list[str]) -> None:
    assert base.said_done(text, base.settings()) == expected


# reactions: read from the reactions table


def test_reactions_section_uses_the_table(world: db.Store) -> None:
    rows = db.messages(conv=[synth.ALICE], limit=None)
    mine = next(r for r in rows if r["outgoing"])
    theirs = next(r for r in rows if not r["outgoing"])
    conn = sqlite3.connect(world.path)
    conn.executemany(
        "INSERT INTO reactions (conv, msg_id, reactor, emoji, reacted_at) VALUES (?, ?, ?, ?, ?)",
        [
            (synth.ALICE, mine["msg_id"], synth.ALICE, THUMBS, data.NOW_TS - 60),
            (synth.ALICE, theirs["msg_id"], synth.ME, HEART, data.NOW_TS - 30),
        ],
    )
    conn.commit()
    conn.close()
    analytics.clear_memory_cache()
    res = analytics.analyze("Alice", names=["reactions"], refresh=True)
    sec = res["sections"]["reactions"]["data"]
    assert sec["total"] == 2
    assert sec["given"] == {"me": 1, "them": 1} and sec["received"] == {"me": 1, "them": 1}
    assert sec["emoji_given"]["them"] == {THUMBS: 1} and sec["emoji_given"]["me"] == {HEART: 1}
    assert {r["who"] for r in sec["top_reactors"]} == {"me", "Alice Example"}
    assert "Reactions: I gave 1 and got 1" in res["summary"]
    assert "reactions" not in res["summary"].split("Not visible:")[-1]
    gaps = {k for k, _ in style.CAPTURE_GAPS}
    assert not gaps & {"reactions", "edits", "deletions"}


# mentions: positions in the original text


def test_mention_context_uses_original_positions() -> None:
    text = "ß" * 300 + " the quarterly report is ready " + "y" * 300
    out = mentions._around(text, "quarterly report")
    assert "quarterly report" in out


# dead helpers removed


def test_dead_pairing_helpers_removed() -> None:
    assert not hasattr(pairing, "texts_of") and not hasattr(pairing, "needs_look")


# insight tools that write local state are not read-only


def test_insight_tool_annotations() -> None:
    from mcp.server.fastmcp import FastMCP

    from wa_mcp_server.tools import insight

    mcp = FastMCP("t")
    insight.register(mcp)
    ann = {t.name: t.annotations for t in asyncio.run(mcp.list_tools())}
    writes = {
        "get_my_day",
        "get_chat_analytics",
        "compare_chat_analytics",
        "list_tasks",
        "refresh_tasks",
        "get_project_overview",
        "list_whats_new",
    }
    for name in writes:
        a = ann[name]
        assert a is not None and a.readOnlyHint is False, name
        assert a.destructiveHint == (name != "compare_chat_analytics"), name
    for name in set(ann) - writes:
        assert ann[name] is not None and ann[name].readOnlyHint is True, name
