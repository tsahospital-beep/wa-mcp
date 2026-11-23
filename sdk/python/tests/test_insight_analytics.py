from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import insight_data as data
import pytest
import synth
from test_insight_tasks import open_world

from wa_mcp import db
from wa_mcp.insight import analytics, base, mentions, pairing, state, wordlists

BUSY = (
    "volume", "reply_speed", "length", "emoji", "stickers", "media", "voice", "quotes", "deferrals",
    "formality", "script", "topics_tasks", "phrases",
)  # fmt: skip


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[db.Store]:
    for name in ("WA_MCP_CONFIG", "WA_MCP_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield open_world(tmp_path)


def test_direct_chat_sections(world: db.Store) -> None:
    res = analytics.analyze("Alice", detail="full")
    sec = res["sections"]
    assert set(BUSY) <= set(sec) and "group_roles" not in sec and "capture_gaps" in sec
    for name in BUSY:
        assert sec[name]["n"] > 0, name
        assert set(sec[name]) == {"n", "low_data", "data", "examples"}
    assert res["meta"]["jids"] == 2 and res["meta"]["cache"] == "miss"
    vol = sec["volume"]["data"]
    assert vol["messages"]["total"] == 48 and vol["sessions"] > 5
    assert set(vol["by_weekday"]) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    rs = sec["reply_speed"]["data"]
    assert rs["me"]["median_min"] is not None and rs["them"]["median_min"] is not None
    assert rs["buckets"]["night_start"] == 1 and rs["buckets"]["over_24h"] >= 0
    assert sec["stickers"]["data"]["total"] == 2 and sec["stickers"]["data"]["by_side"] == {
        "them": 1,
        "me": 1,
    }
    voice = sec["voice"]["data"]
    assert (
        voice["transcribed"] == 2 and voice["untranscribed"] == 2 and voice["by_side"] == {"them": 3, "me": 1}
    )
    assert sec["quotes"]["data"]["n"] == {"me": 1, "them": 1}
    assert sec["emoji"]["data"]["them"]["per_100_messages"] > 0
    assert sec["emoji"]["data"]["them"]["laugh_per_100"] > 0
    assert sec["script"]["data"]["me"]["main"] == "latin"
    assert sec["media"]["data"]["images_with_words_nearby"] == "1/1"
    assert sec["deferrals"]["data"]["deferred_but_read"] >= 1
    assert sec["topics_tasks"]["data"]["requests"] == 3
    assert any("library" in p["phrase"] for p in sec["phrases"]["data"]["mine"])
    assert sec["reactions"]["data"]["total"] == 0 and sec["reactions"]["data"]["top_reactors"] == []
    assert res["summary"].startswith("Chat with Alice Example: 48 messages")
    assert "!" not in res["summary"]


def test_group_roles_and_detail_levels(world: db.Store) -> None:
    res = analytics.analyze("Book club", detail="brief")
    roles = res["sections"]["group_roles"]["data"]
    assert roles["participant_count"] == 4 and roles["aimed_at_me"] == 1
    assert {p["who"] for p in roles["participants"]} == {"Alice Example", "Bobby", "Carol", "me"}
    assert "by_month" not in res["sections"]["volume"]["data"]
    assert all(len(s["examples"]) <= 1 for s in res["sections"].values())
    full = analytics.analyze("Book club", detail="full")
    assert "by_month" in full["sections"]["volume"]["data"] and full["meta"]["cache"] == "hit"
    with pytest.raises(ValueError):
        analytics.analyze("Book club", detail="huge")
    with pytest.raises(ValueError):
        analytics.analyze("Book club", names=["nope"])


def test_examples_are_masked_and_secret_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_CONFIG", raising=False)
    b = data.two_weeks()
    for i in range(4):
        b.add(
            synth.BOB,
            synth.BOB,
            data.at(13, 12, i * 10),
            "My number is +1 555 010 0199, see you at the library",
        )
        b.add(synth.BOB, data.ME, data.at(13, 12, i * 10 + 5), "the password: hunter2 for the library wifi")
    open_world(tmp_path, b)
    res = analytics.analyze("Bob", detail="full")
    raw = json.dumps(res)
    assert "0100199" not in raw and "010 0199" not in raw
    assert "hunter2" not in raw


def test_cache_is_invalidated_by_new_data(world: db.Store) -> None:
    first = analytics.analyze("Alice", names=["volume"])
    assert first["meta"]["cache"] == "miss"
    assert analytics.analyze("Alice", names=["volume"])["meta"]["cache"] == "hit"
    analytics.clear_memory_cache()
    again = analytics.analyze("Alice", names=["volume"])
    assert again["meta"]["cache"] == "hit" and again["sections"] == first["sections"]
    conn = sqlite3.connect(world.path)
    conn.execute(
        "INSERT INTO chat_messages (conv, msg_id, author, sent_at, body) VALUES (?, 'NEW1', ?, ?, 'one more')",
        (synth.ALICE, synth.ALICE, data.NOW_TS - 60),
    )
    conn.commit()
    conn.close()
    fresh = analytics.analyze("Alice", names=["volume"])
    assert fresh["meta"]["cache"] == "miss"
    assert fresh["sections"]["volume"]["data"]["messages"]["total"] == 49
    with sqlite3.connect(world.path) as edit:
        edit.execute("UPDATE chat_messages SET body = 'revised' WHERE msg_id = 'NEW1'")
    assert analytics.analyze("Alice", names=["volume"])["meta"]["cache"] == "miss"
    voice_id = next(
        m["msg_id"] for m in data.two_weeks().msgs if m["kind"] == "voice" and m["author"] == data.ME
    )
    db.put_derived(synth.ALICE, voice_id, "transcript", "on my way", lang="en", confidence=0.9)
    assert analytics.analyze("Alice", names=["volume"])["meta"]["cache"] == "miss"
    assert analytics.analyze("Alice", names=["volume"], refresh=True)["meta"]["cache"] == "miss"
    with state.connect() as conn2:
        assert conn2.execute("SELECT COUNT(*) FROM analytics_cache").fetchone()[0] >= 3


def test_report_file(world: db.Store) -> None:
    res = analytics.analyze("Alice", detail="brief", report=True)
    path = Path(res["meta"]["report_path"])
    assert path.parent == world.cfg.exports_dir
    assert re.fullmatch(r"analytics_Alice_Example_[0-9a-f]{8}\.md", path.name)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Chat analytics: Alice Example") and "## reply_speed" in text


def test_compare(world: db.Store) -> None:
    res = analytics.compare(["Alice", "Bob", "Book club"])
    assert [r["name"] for r in res["rows"]] == ["Alice Example", "Bobby", "Book club"]
    assert res["differences"]
    assert {d["metric"] for d in res["differences"]} >= {"reply_median_min"}
    assert all(set(d) >= {"chat", "metric", "value"} for d in res["differences"])
    with pytest.raises(ValueError):
        analytics.compare(["Alice"])
    missing = analytics.compare(["Alice", "Nobody at all"])
    assert "error" in missing["rows"][1]


def test_mentions(world: db.Store) -> None:
    res = mentions.find(days=30, now=data.NOW_TS)
    assert res["by_watch"] == {"updated report": 1, "Library": 1}
    report = next(m for m in res["mentions"] if "updated report" in m["watch"])
    assert report["message_id"] == data.IDS["report"] and report["from"] == "Alice Example"
    assert report["context"]["after"] == "Sure, will do"
    mine = mentions.find(days=30, words=["library"], include_me=True, now=data.NOW_TS)
    assert mine["count"] == 11 and mine["watch"] == ["library"]
    page = mentions.find(days=30, words=["library"], include_me=True, limit=4, offset=8, now=data.NOW_TS)
    assert len(page["mentions"]) == 3 and page["next_offset"] == 11
    assert mentions.find(days=1, now=data.NOW_TS)["count"] == 0


def test_pairing(world: db.Store) -> None:
    msgs = base.load(base.target("Alice").jids)
    pairs = pairing.pair_media(msgs)
    cover = pairs[data.IDS["cover"]]
    assert cover.how == ["after"] and cover.text == "this one looks better for the poster"
    t = data.NOW_TS
    seq = [
        base.Msg(synth.BOB, "p1", synth.BOB, t, "image", ""),
        base.Msg(synth.BOB, "p2", synth.BOB, t + 5, "image", "second one"),
        base.Msg(synth.BOB, "q", base.ME, t + 20, "text", "which?"),
        base.Msg(synth.BOB, "w", synth.BOB, t + 40, "text", "the first is for the cover"),
        base.Msg(synth.BOB, "long", base.ME, t + 60, "text", "x" * 60),
        base.Msg(synth.BOB, "late", synth.BOB, t + 90, "text", "and something else"),
        base.Msg(synth.BOB, "r", base.ME, t + 900, "text", "love it", reply_to="p1"),
    ]
    got = pairing.pair_media(seq)
    assert got["p1"].how == ["after", "reply"] and got["p1"].texts == [
        "the first is for the cover",
        "love it",
    ]
    assert got["p2"].how == ["caption", "after"]
    assert pairing.media_line(seq[0], got["p1"]).startswith("image: about 'the first is for the cover")


def test_text_helpers(world: db.Store) -> None:
    flag = chr(0x1F1FA) + chr(0x1F1F8)
    family = chr(0x1F468) + chr(0x200D) + chr(0x1F469) + chr(0x200D) + chr(0x1F467)
    thumbs = chr(0x1F44D) + chr(0x1F3FD)
    assert base.emoji_clusters(f"hi {flag} {family}{thumbs} 1{chr(0xFE0F)}{chr(0x20E3)}") == [
        flag,
        family,
        thumbs,
        "1" + chr(0xFE0F) + chr(0x20E3),
    ]
    assert base.script_of("hello there") == "latin"
    assert base.script_of(chr(0x0410) + chr(0x0411) + chr(0x0412) + " ab") == "cyrillic"
    assert base.script_of("12 34") == "none"
    assert base.mask("call +1 555 010 0123 or 15550100123@s.whatsapp.net on 2026-03-16") == (
        "call ***23 or ***23@s.whatsapp.net on 2026-03-16"
    )
    assert base.mask("see https://example.com/doc/very/long/path?key=abc") == "see example.com/..."
    for secret in ("my password: abc123", "token sk-abcdef1234567890", "code is 4821", "  948213 "):
        assert base.looks_secret(secret), secret
    assert not base.looks_secret("the dress code is casual")
    assert wordlists.Words(["deliver*", "set up"]).find("Delivered it, then set   up the room") == [
        "deliver*",
        "set up",
    ]
