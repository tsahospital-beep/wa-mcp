from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import insight_data as data
import pytest
import synth

from wa_mcp import config, db, people
from wa_mcp.insight import analytics, base, tasks, tracker
from wa_mcp.insight.base import ME, Msg


def open_world(root: Path, builder: data.Builder | None = None, **cfg: object) -> db.Store:
    path = synth.make_config(root, **(cfg or data.CONFIG))
    (builder or data.two_weeks()).write(root)
    store = db.connect(config.load(path))
    people.connect(store=store)
    analytics.clear_memory_cache()
    return store


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[db.Store]:
    for name in ("WA_MCP_CONFIG", "WA_MCP_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield open_world(tmp_path)


def msg(text: str, who: str = synth.ALICE, ts: int = data.NOW_TS, kind: str = "text", **extra: object) -> Msg:
    return Msg(conv=synth.ALICE, id=f"x{ts}{len(text)}", who=who, ts=ts, kind=kind, body=text, **extra)


def test_bursts_split_on_gap_sender_and_my_messages(world: db.Store) -> None:
    t = data.NOW_TS
    msgs = [
        msg("one", ts=t),
        msg("two", ts=t + 60),
        msg("three", ts=t + 400),
        msg("from bob", who=synth.BOB, ts=t + 420),
        msg("mine", who=ME, ts=t + 430),
        msg("again", who=synth.BOB, ts=t + 440),
    ]
    groups = tasks.bursts(msgs, gap_minutes=4)
    assert [[m.body for m in g] for g in groups] == [["one", "two"], ["three"], ["from bob"], ["again"]]
    assert len(tasks.bursts(msgs, gap_minutes=10)[0]) == 3


@pytest.mark.parametrize(
    ("texts", "category", "low", "high"),
    [
        (["Please send me the slides today"], "NEW_TASK", 90, 97),
        (["Could you check the invoice"], "NEW_TASK", 75, 85),
        (["The website is broken, the logo is missing"], "FIX", 85, 95),
        (["Any update on this?"], "FOLLOW_UP", 20, 20),
        (["Did you send it?"], "FOLLOW_UP", 20, 20),
        (["Never mind, no need"], "CANCEL", 10, 10),
        (["We need a new logo for the shop asap"], "NEW_TASK", 95, 97),
        (["Changes:\n1. title\n2. colors\n3. footer"], "NEW_TASK", 95, 95),
        (["https://example.com/page"], "PRECURSOR", 35, 35),
        (["Thanks, see you"], "CHAT", 5, 5),
        (["Where?"], "CHAT", 20, 20),
    ],
)
def test_score_burst_categories(
    world: db.Store, texts: list[str], category: str, low: int, high: int
) -> None:
    v = tasks.score_burst([msg(t, ts=data.NOW_TS + i) for i, t in enumerate(texts)], base.settings())
    assert v.category == category, v
    assert low <= v.score <= high and v.reasons


def test_score_burst_media_and_optional_cancel(world: db.Store) -> None:
    s = base.settings()
    doc = tasks.score_burst([msg("", kind="document", file_name="scan.pdf")], s)
    assert (doc.category, doc.score) == ("PRECURSOR", 60)
    spec = tasks.score_burst([msg("", kind="document", file_name="brief.pdf")], s)
    assert (spec.category, spec.score) == ("NEW_TASK", 95) and spec.reasons[0] == "spec file attached"
    image = tasks.score_burst([msg("", kind="image")], s)
    assert (image.category, image.score) == ("PRECURSOR", 30)
    voice = tasks.score_burst([msg("", kind="voice"), msg("", kind="voice", ts=data.NOW_TS + 5)], s)
    assert (voice.category, voice.score) == ("VOICE", 65)
    heard = tasks.score_burst([msg("", kind="voice", transcript="please send the photos today")], s)
    assert heard.category == "NEW_TASK" and "read from a voice transcript" in heard.reasons
    soft = tasks.score_burst([msg("Can you fix the page? If not, never mind")], s)
    assert soft.category != "CANCEL"


def test_detect_in_direct_chat_and_group(world: db.Store) -> None:
    alice = tasks.detect_tasks(synth.ALICE)
    cats = [(d["category"], d["text"]) for d in alice["detections"] if d["category"] != "CHAT"]
    assert ("NEW_TASK", "Can you please send me the updated report by Friday?") in cats
    report = next(d for d in alice["detections"] if d["ids"] == [data.IDS["report"]])
    assert report["score"] == 98 and any("you accepted" in r for r in report["reasons"])
    logo = next(d for d in alice["detections"] if d["ids"] == [data.IDS["logo"]])
    assert logo["category"] == "FIX" and logo["project"]["id"] == "club-site"
    bob = tasks.detect_tasks(synth.BOB)
    banner = next(d for d in bob["detections"] if data.IDS["banner"] in d["ids"])
    assert len(banner["ids"]) == 3 and banner["category"] == "NEW_TASK"
    assert not any(data.IDS["flyer"] in d["ids"] and len(d["ids"]) > 1 for d in bob["detections"])
    group = tasks.detect_tasks("Book club")
    room = next(d for d in group["detections"] if d["ids"] == [data.IDS["room"]])
    assert room["category"] == "NEW_TASK" and "mentions you" in room["to_me"]
    handouts = next(d for d in group["detections"] if d["ids"] == [data.IDS["handouts"]])
    assert handouts["category"] == "TEAM" and handouts["score"] < 40
    assert tasks.detect_tasks(synth.ALICE, min_score=90)["counts"] == {"NEW_TASK": 1, "FIX": 1}


def test_tracker_statuses(world: db.Store) -> None:
    res = tracker.refresh(now=data.NOW_TS)
    assert res["tasks"] == 4 and res["open"] == 3
    items = {t["title"]: t for t in tracker.overview(open_only=False)["tasks"]}
    report = items["Can you please send me the updated report by Friday?"]
    assert report["status"] == "new" and "complaint after delivery" in report["status_why"]
    assert report["pings"] == 1 and report["due"].startswith("by friday")
    kinds = [st["kind"] for st in tracker.detail(report["id"])["steps"]]
    assert kinds == ["request", "accepted", "follow_up", "delivery", "thanks", "fix"]
    banner = items["I need a new banner"]
    assert (
        banner["status"] == "in_progress" and banner["status_why"] == "the requester answered your question"
    )
    assert banner["project"] == {"id": "club-site", "name": "Book club site"}
    room = items["Can you book the room for Thursday please"]
    assert room["status"] == "done" and room["from"] == "Carol"
    logo = items["The logo on the website looks broken, please fix it"]
    assert logo["status"] == "new" and logo["category"] == "FIX"
    assert not any("handouts" in t for t in items)
    overview = tracker.overview()
    assert overview["by_status"] == {"new": 2, "in_progress": 1}
    assert overview["tasks"][0]["status"] == "new"
    found = tracker.which(data.IDS["report_ping"])
    assert found["found"] and found["task"]["id"] == report["id"] and found["step"]["kind"] == "follow_up"
    assert tracker.which("nope")["found"] is False
    assert tracker.detail(query="banner")["task"]["id"] == banner["id"]
    proj = tracker.project_overview("Book club site")
    assert proj["configured"] and proj["tasks"] == 2 and proj["by_status"] == {"new": 1, "in_progress": 1}


def test_tracker_refresh_is_idempotent_and_whats_new(world: db.Store) -> None:
    tracker.refresh(now=data.NOW_TS)
    first = tracker.overview(open_only=False)
    tracker.refresh(now=data.NOW_TS)
    assert tracker.overview(open_only=False) == first
    news = tracker.whats_new(hours=30, now=data.NOW_TS)
    kinds = {(s["kind"], s["task"][:12]) for s in news["steps"]}
    assert ("request", "The logo on ") in kinds and ("follow_up", "I need a new") in kinds
    assert news["new_tasks"] == 1
    assert tracker.ensure_fresh(now=data.NOW_TS + 10) is False
    with pytest.raises(ValueError):
        tracker.whats_new(hours=0)


def test_tracker_does_not_copy_text_when_encrypted(world: db.Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db.Store, "encrypted", property(lambda self: True))
    tracker.refresh(now=data.NOW_TS)
    conn = sqlite3.connect(world.cfg.insight_db)
    titles = [r[0] for r in conn.execute("SELECT title FROM tasks")]
    texts = [r[0] for r in conn.execute("SELECT text FROM task_steps")]
    conn.close()
    assert set(titles) == {""} and set(texts) == {""}
    shown = tracker.overview(open_only=False)["tasks"]
    assert any(t["title"].startswith("Can you please send me") for t in shown)
