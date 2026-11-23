"""Regression tests for review findings on the insight layer."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import insight_data as data
import pytest
import synth
from insight_data import ME, at
from test_insight_tasks import open_world

from wa_mcp import config, db, people
from wa_mcp.insight import analytics, base, mentions, my_day, state, tracker, when

CAROL_PHONE = "15550100004@s.whatsapp.net"
DAY = 86400


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[db.Store]:
    for name in ("WA_MCP_CONFIG", "WA_MCP_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield open_world(tmp_path)


def _texts(store: db.Store) -> list[str]:
    conn = sqlite3.connect(store.cfg.insight_db)
    out = []
    for table, cols in (
        ("tasks", "title, status_why, next_step, due_hint"),
        ("task_steps", "text, why"),
        ("analytics_cache", "payload"),
    ):
        for row in conn.execute(f"SELECT {cols} FROM {table}"):
            out += [str(v) for v in row if v]
    conn.close()
    return out


def test_parse_due_caps_far_future_and_overflow(world: db.Store) -> None:
    ref = datetime(2026, 3, 16, 12, 0)
    assert when.parse_due("in 3650 days", ref) is not None
    assert when.parse_due("in 99999999 days", ref) is None
    assert when.parse_due("in 600 weeks", ref) is None
    assert when.parse_due("tomorrow", datetime(9999, 12, 31, 12)) is None
    assert when.parse_due("by 9999-12-31", ref) is None
    assert when.parse_due("by 2035-03-01", ref) is not None


def test_parse_due_feb_29(world: db.Store) -> None:
    leap = datetime(2028, 6, 1, 12)
    assert when.parse_due("on 29/02", leap).day == date(2032, 2, 29)  # type: ignore[union-attr]
    assert when.parse_due("by February 29", leap).day == date(2032, 2, 29)  # type: ignore[union-attr]
    common = datetime(2027, 1, 10, 12)
    assert when.parse_due("by February 29", common).day == date(2028, 2, 29)  # type: ignore[union-attr]
    assert when.parse_due("on 29.02.2027", common) is None
    assert when.parse_due("by February 29", datetime(2028, 1, 10)).day == date(2028, 2, 29)  # type: ignore[union-attr]


def test_reasons_and_next_steps_never_hold_message_text(
    world: db.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker.refresh(now=data.NOW_TS)
    allowed = tuple(tracker.NEXT_STEP.values())
    for t in tracker.overview(open_only=False)["tasks"]:
        step = t["next_step"].split(". ")[-1]
        assert not step or step.startswith(allowed), t["next_step"]
        assert "Alice" not in t["next_step"] and "Bob" not in t["next_step"]
    monkeypatch.setattr(db.Store, "encrypted", property(lambda self: True))
    tracker.refresh(now=data.NOW_TS)
    stored = " ".join(_texts(world)).casefold()
    for word in ("charts", "sure", "pixels", "report.pdf", "1200", "friday", "which size", "agenda", "room"):
        assert word not in stored, word


def test_encryption_wipes_existing_text_once(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker.refresh(now=data.NOW_TS)
    analytics.analyze("Alice", names=["volume"])
    assert any("charts" in t for t in _texts(world))
    monkeypatch.setattr(db.Store, "encrypted", property(lambda self: True))
    with state.connect() as conn:
        assert state.get_meta(conn, "text_mode") == "sealed"
        assert conn.execute("SELECT COUNT(*) FROM analytics_cache").fetchone()[0] == 0
        assert {r[0] for r in conn.execute("SELECT title FROM tasks")} == {""}
        assert {r[0] for r in conn.execute("SELECT text FROM task_steps")} == {""}
    shown = tracker.overview(open_only=False)["tasks"]
    assert any(
        t["title"].startswith("The report file is broken") or t["title"].startswith("Can you") for t in shown
    )
    assert all(t["due"] is None or t["due"].startswith("due 20") for t in shown)


def test_retention_for_cache_and_closed_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    store = open_world(tmp_path, tasks={**data.CONFIG["tasks"], "keep_days": 1}, insight={"cache_days": 5})
    with state.connect() as conn, state.transaction(conn):
        conn.execute(
            "INSERT INTO analytics_cache VALUES ('old', 'x', ?, '{}')",
            (int(datetime.now().timestamp()) - 9 * DAY,),
        )
    analytics.analyze("Alice", names=["volume"])
    tracker.refresh(now=data.NOW_TS)
    with state.connect() as conn:
        keys = {r[0] for r in conn.execute("SELECT key FROM analytics_cache")}
        statuses = sorted(r[0] for r in conn.execute("SELECT status FROM tasks"))
    assert "old" not in keys and len(keys) == 1
    assert statuses == ["in_progress", "new", "new"]
    assert store.cfg.insight_db.is_file()


def test_rows_follow_a_new_lid_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    b = data.two_weeks()
    b.add(synth.CAROL_LID, synth.CAROL_LID, at(13, 15), "Please send me the agenda today")
    store = open_world(tmp_path, b)
    tracker.refresh(now=data.NOW_TS)
    with state.connect() as conn, state.transaction(conn):
        conn.execute(
            "INSERT INTO tasks (id, chat, asker, category, score, status, first_conv, first_msg, created_at, "
            "updated_at) VALUES ('told', ?, ?, 'NEW_TASK', 90, 'done', ?, 'gone', ?, ?)",
            (
                synth.CAROL_LID,
                synth.CAROL_LID,
                synth.CAROL_LID,
                data.NOW_TS - 40 * DAY,
                data.NOW_TS - 39 * DAY,
            ),
        )
    before = {t["chat"] for t in tracker.overview(open_only=False)["tasks"]}
    assert synth.CAROL_LID in before and CAROL_PHONE not in before
    conn = sqlite3.connect(store.cfg.session_db)
    conn.execute("INSERT INTO whatsmeow_lid_map VALUES ('200000000000004', '15550100004')")
    conn.commit()
    conn.close()
    synth.sync_identities(store.cfg.session_db.parent)
    people.connect(store=store)
    tracker.refresh(now=data.NOW_TS)
    rows = tracker.overview(open_only=False)["tasks"]
    chats = [t["chat"] for t in rows]
    assert synth.CAROL_LID not in chats
    assert chats.count(CAROL_PHONE) == 2
    agenda = [t for t in rows if t["title"] == "Please send me the agenda today"]
    assert len(agenda) == 1 and agenda[0]["from_jid"] == CAROL_PHONE
    room = next(t for t in rows if t["title"].startswith("Can you book the room"))
    assert room["from_jid"] == CAROL_PHONE
    only = tracker.overview(open_only=False, chat=[synth.CAROL_LID, CAROL_PHONE])
    assert only["count"] == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("not ready yet", []),
        ("it isn't sent", []),
        ("Done? Not yet", []),
        ("I haven't sent it", []),
        ("is it done yet", []),
        ("done, sent it", ["done", "sent"]),
        ("I'm not sure, but it is done", ["done"]),
        ("it is done" + chr(0x2019), ["done"]),
    ],
)
def test_done_words_respect_negation(world: db.Store, text: str, expected: list[str]) -> None:
    assert base.said_done(text, base.settings()) == expected


def test_negated_done_does_not_close_a_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    b = data.Builder()
    b.add(synth.ALICE, synth.ALICE, at(13, 9), "Please send me the slides today")
    b.add(synth.ALICE, ME, at(13, 9, 30), "The slides are not ready yet")
    open_world(tmp_path, b, tasks={"projects": []})
    tracker.refresh(now=data.NOW_TS)
    task = tracker.overview()["tasks"][0]
    assert task["status"] == "new"
    b.add(synth.ALICE, ME, at(13, 18), "Done, the slides are in your mail")
    open_world(tmp_path / "again", b, tasks={"projects": []})
    tracker.refresh(now=data.NOW_TS)
    assert tracker.overview(open_only=False)["tasks"][0]["status"] == "done"


def test_open_tasks_go_stale_and_leave_my_day(world: db.Store) -> None:
    tracker.refresh(now=data.NOW_TS)
    later = data.NOW_TS + 70 * DAY
    tracker.refresh(now=later)
    assert tracker.overview()["count"] == 0
    stale = tracker.overview(status="stale")
    assert stale["count"] == 3
    assert all(
        t["status_why"] == "no activity for more than 60 days" and t["next_step"] == ""
        for t in stale["tasks"]
    )
    tracker.refresh(days=100, now=later)
    assert tracker.overview(status="stale")["count"] == 3
    day = my_day.my_day(datetime.fromtimestamp(later).date(), now=later, refresh=False, lookback_days=100)
    assert day["counts"]["open_tasks"] == 0


def test_migrations_are_atomic_and_repeatable(world: db.Store) -> None:
    fresh = world.cfg.store_dir / "fresh_insight.db"
    errors: list[BaseException] = []
    results: list[int] = []

    def run() -> None:
        try:
            conn = sqlite3.connect(fresh, timeout=10, isolation_level=None)
            results.append(state.migrate(conn))
            conn.close()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results == [len(state.MIGRATIONS)] * 4
    half = world.cfg.store_dir / "half_insight.db"
    conn = sqlite3.connect(half, isolation_level=None)
    for sql in state.MIGRATIONS[0][:3]:
        conn.execute(sql)
    assert state.migrate(conn) == len(state.MIGRATIONS)
    assert state.migrate(conn) == len(state.MIGRATIONS)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert {"insight_meta", "analytics_cache", "tasks", "task_steps"} <= names


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"tasks": {"burst_gap_minutes": -1}}, "tasks.burst_gap_minutes"),
        ({"tasks": {"min_score": 500}}, "tasks.min_score"),
        ({"tasks": {"refresh_days": 2.5}}, "whole number"),
        ({"tasks": {"stale_days": "x"}}, "tasks.stale_days"),
        ({"tasks": {"keywords": {"bogus": ["x"]}}}, "unknown lists"),
        ({"tasks": {"keywords": ["x"]}}, "tasks.keywords"),
        ({"tasks": {"projects": [{"id": "a"}, {"id": "a"}]}}, "duplicate id"),
        ({"insight": {"night_hours": [25, 3]}}, "insight.night_hours"),
        ({"insight": {"words": {"deferral": 5}}}, "insight.words.deferral"),
        ({"insight": {"dates": {"weekdays": ["mon"]}}}, "insight.dates.weekdays"),
        ({"insight": {"dates": {"someday": ["x"]}}}, "unknown keys"),
        ({"insight": {"cache_days": 0}}, "insight.cache_days"),
        ({"mentions": {"watch": "report"}}, "mentions.watch"),
    ],
)
def test_settings_are_validated(tmp_path: Path, extra: dict[str, Any], message: str) -> None:
    path = synth.make_config(tmp_path, **extra)
    db.connect(config.load(path))
    with pytest.raises(ValueError, match=message):
        base.settings()


def test_settings_are_built_once_per_config(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    first = base.settings()
    calls: list[int] = []
    monkeypatch.setattr(base, "_build", lambda cfg: calls.append(1))
    assert base.settings() is first and not calls


def test_day_windows_use_next_midnight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    for day in (date(2026, 3, 8), date(2026, 3, 29), date(2026, 10, 25), date(2026, 11, 1)):
        start, end = when.day_bounds(day)
        assert start == int(datetime(day.year, day.month, day.day).timestamp())
        assert end == when.day_bounds(day + timedelta(days=1))[0]
    b = data.Builder()
    b.schedule(synth.GROUP, "late one", datetime(2026, 3, 16, 23, 50))
    b.schedule(synth.GROUP, "next day", datetime(2026, 3, 17, 0, 5))
    b.add(synth.GROUP, synth.BOB, at(13, 9), "hello")
    open_world(tmp_path, b, tasks={"projects": []})
    res = my_day.my_day("2026-03-16", now=data.NOW_TS, refresh=False)
    sends = [i["context"] for g in res["people"] for i in g["items"] if i["type"] == "scheduled"]
    assert sends == ["late one"]


def test_mentions_page_in_sql(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    real = db.messages

    def spy(**kw: Any) -> list[db.Row]:
        seen.append(kw)
        return real(**kw)

    monkeypatch.setattr(db, "messages", spy)
    first = mentions.find(days=30, words=["library"], include_me=True, limit=4, now=data.NOW_TS)
    assert first["count"] == 4 and first["has_more"] is True
    assert all(kw["query"] == "library" and kw["limit"] == 200 and kw["after"] for kw in seen)
    last = mentions.find(days=30, words=["library"], include_me=True, limit=4, offset=8, now=data.NOW_TS)
    assert last["count"] == 3 and last["has_more"] is False
    times = [m["time"] for m in first["mentions"] + last["mentions"]]
    assert times == sorted(times, reverse=True)


def test_cache_check_counts_without_reading_text(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kw: Any) -> None:
        raise AssertionError("derived text must not be read for the cache check")

    monkeypatch.setattr(db, "derived_by_source", boom)
    assert analytics.analyze("Alice", names=["volume"])["meta"]["cache"] == "miss"
    assert analytics.analyze("Alice", names=["volume"])["meta"]["cache"] == "hit"


def test_brief_detail_keeps_meta_whole(world: db.Store) -> None:
    res = analytics.analyze("Alice", detail="brief")
    assert len(res["meta"]["sections"]) > 5 and res["meta"]["sections"][-1] == "capture_gaps"
    assert len(res["meta"]["period"]) == 2
