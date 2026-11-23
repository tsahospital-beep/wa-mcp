from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import insight_data as data
import pytest
import synth
from insight_data import ME, at
from test_insight_tasks import open_world

from wa_mcp import db
from wa_mcp.insight import my_day, tracker, when


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[db.Store]:
    for name in ("WA_MCP_CONFIG", "WA_MCP_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield open_world(tmp_path)


REF = datetime(2026, 3, 16, 12, 0)  # Monday


@pytest.mark.parametrize(
    ("text", "expected", "all_day"),
    [
        ("I will send it tomorrow", datetime(2026, 3, 17), True),
        ("will do it today", datetime(2026, 3, 16), True),
        ("you'll have it by 5 pm", datetime(2026, 3, 16, 17, 0), False),
        ("call you at 9:30", datetime(2026, 3, 17, 9, 30), False),
        ("I'll send it on Friday", datetime(2026, 3, 20), True),
        ("next friday works", datetime(2026, 3, 27), True),
        ("on Monday", datetime(2026, 3, 23), True),
        ("this monday", datetime(2026, 3, 16), True),
        ("next week", datetime(2026, 3, 23), True),
        ("tomorrow morning", datetime(2026, 3, 17, 10, 0), False),
        ("tonight", datetime(2026, 3, 16, 20, 0), False),
        ("by end of day", datetime(2026, 3, 16, 18, 0), False),
        ("tomorrow at 3pm", datetime(2026, 3, 17, 15, 0), False),
        ("deadline 2026-04-02", datetime(2026, 4, 2), True),
        ("by March 20th", datetime(2026, 3, 20), True),
        ("on the 5th of April", datetime(2026, 4, 5), True),
        ("due 20/03", datetime(2026, 3, 20), True),
        ("in 2 hours", datetime(2026, 3, 16, 14, 0), False),
        ("in a few days", datetime(2026, 3, 19, 12, 0), True),
        ("the day after tomorrow", datetime(2026, 3, 18), True),
    ],
)
def test_parse_due(world: db.Store, text: str, expected: datetime, all_day: bool) -> None:
    due = when.parse_due(text, REF)
    assert due is not None, text
    assert due.at == expected and due.all_day is all_day


def test_parse_due_ignores_text_without_time(world: db.Store) -> None:
    for text in ("thanks a lot", "version 2.1 is out", "the sun is out", "I may join", ""):
        assert when.parse_due(text, REF) is None, text


def test_my_day_groups_everything(world: db.Store) -> None:
    tracker.refresh(now=data.NOW_TS)
    res = my_day.my_day("2026-03-16", now=data.NOW_TS, refresh=False)
    c = res["counts"]
    assert c["promises_due"] == 2 and c["promises_overdue"] == 1
    assert c["open_tasks"] == 3 and c["tasks_due"] == 1
    assert c["missed_calls"] == 1 and c["scheduled"] == 1
    assert c["waiting_for_reply"] == 2 and c["questions"] == 2
    people = {g["chat"]: g for g in res["people"]}
    assert res["people"][0]["chat"] == synth.BOB
    bob = [i["type"] for i in people[synth.BOB]["items"]]
    assert bob == ["promise", "missed_call", "waiting_reply", "task"]
    overdue = people[synth.BOB]["items"][0]
    assert overdue["status"] == "overdue" and overdue["message_id"] == data.IDS["promise_overdue"]
    assert overdue["due"].startswith("on friday")
    alice = people[synth.ALICE]["items"]
    promise = next(i for i in alice if i["type"] == "promise")
    assert promise["status"] == "due_today" and promise["message_id"] == data.IDS["promise_today"]
    assert {i["task_id"] for i in alice if i["type"] == "task"}
    assert synth.CAROL_LID not in people
    assert [i["type"] for i in people[synth.GROUP]["items"]] == ["scheduled"]
    for g in res["people"]:
        for item in g["items"]:
            assert {"chat", "message_id", "time", "context"} <= set(item)
    assert res["summary"].startswith("2026-03-16: 3 open request(s)")
    only = my_day.my_day("2026-03-16", chat="Bob", now=data.NOW_TS, refresh=False)
    assert [g["chat"] for g in only["people"]] == [synth.BOB]


def test_my_day_on_an_earlier_day(world: db.Store) -> None:
    tracker.refresh(now=data.NOW_TS)
    res = my_day.my_day(date(2026, 3, 13), now=data.NOW_TS, refresh=False)
    promises = [i for g in res["people"] for i in g["items"] if i["type"] == "promise"]
    assert [p["message_id"] for p in promises] == [data.IDS["promise_overdue"]]
    assert promises[0]["status"] == "due_today"
    with pytest.raises(ValueError):
        my_day.my_day("13.03.2026", now=data.NOW_TS, refresh=False)


def test_waiting_order_and_missed_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    b = data.Builder()
    b.add(synth.ALICE, ME, at(12, 9), "Morning")
    b.add(synth.ALICE, synth.ALICE, at(12, 10), "Can you send the photos?")
    b.add(synth.BOB, ME, at(14, 8), "Hello")
    b.add(synth.BOB, synth.BOB, at(14, 9), "Are you free later?")
    b.add(synth.CAROL_LID, ME, at(11, 8), "Hi")
    b.add(synth.CAROL_LID, synth.CAROL_LID, at(11, 9), "Here is the summary of the meeting we had yesterday")
    b.add(synth.GROUP, synth.BOB, at(14, 10), "Anyone up for coffee?")
    b.call("K1", synth.ALICE, at(14, 7), "missed")
    b.call("K2", synth.BOB, at(13, 7), "missed")
    b.add(synth.BOB, ME, at(13, 7, 30), "Sorry, missed your call")
    b.call("K3", synth.CAROL_LID, at(13, 8), "rejected")
    open_world(tmp_path, b, tasks={"projects": []})
    res = my_day.my_day("2026-03-16", now=data.NOW_TS, refresh=False)
    waiting = [i for g in res["people"] for i in g["items"] if i["type"] == "waiting_reply"]
    assert [(w["chat"], w["question"]) for w in waiting] == [
        (synth.ALICE, True),
        (synth.BOB, True),
        (synth.CAROL_LID, False),
    ]
    assert waiting[0]["waiting_minutes"] > waiting[1]["waiting_minutes"]
    missed = [i for g in res["people"] for i in g["items"] if i["type"] == "missed_call"]
    assert [(m["chat"], m["missed"]) for m in missed] == [(synth.ALICE, 1)]
    assert res["people"][0]["chat"] == synth.ALICE
    assert synth.GROUP not in {g["chat"] for g in res["people"]}


def test_promise_kept_is_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    b = data.Builder()
    b.add(synth.ALICE, ME, at(13, 9), "I will send the photos tomorrow")
    b.add(synth.ALICE, ME, at(14, 9), "", "image", attachment_name="p.jpg")
    b.add(synth.BOB, ME, at(13, 9), "I'll call you tomorrow at 5pm")
    open_world(tmp_path, b, tasks={"projects": []})
    res = my_day.my_day("2026-03-16", now=data.NOW_TS + 6 * 3600, refresh=False)
    promises = [i for g in res["people"] for i in g["items"] if i["type"] == "promise"]
    assert [(p["chat"], p["status"]) for p in promises] == [(synth.BOB, "overdue")]
    assert (datetime.fromisoformat(res["as_of"]) - data.NOW).total_seconds() == 6 * 3600
