"""Two weeks of synthetic chats for the insight tests: Alice and Bob (direct), Carol (LID only), group "Book club".

All people and numbers are fictional. The clock is fixed: NOW is Monday 2026-03-16 12:00 local time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import synth
from synth import ALICE, BOB, CAROL_LID, GROUP, ME

NOW = datetime(2026, 3, 16, 12, 0)
NOW_TS = int(NOW.timestamp())
DAY0 = datetime(2026, 3, 2)
TITLES = {ALICE: "Alice", BOB: "Bob", CAROL_LID: "", GROUP: "Book club"}
GRIN = chr(0x1F600)
TEARS = chr(0x1F602)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Local time on day N of the two weeks (day 0 is Monday 2026-03-02)."""
    return DAY0 + timedelta(days=day, hours=hour, minutes=minute)


class Builder:
    def __init__(self) -> None:
        self.msgs: list[dict[str, Any]] = []
        self.derived: list[tuple[str, str, str, str, float, str, int]] = []
        self.calls: list[tuple[str, str, Any, int, int, int, Any, str]] = []
        self.scheduled: list[tuple[str, str, int, str, int]] = []
        self.n = 0

    def add(
        self, conv: str, who: str, when: datetime, body: str = "", kind: str = "text", **extra: Any
    ) -> str:
        self.n += 1
        mid = extra.pop("id", f"M{self.n:04d}")
        self.msgs.append(
            {
                "conv": conv,
                "msg_id": mid,
                "author": who,
                "outgoing": int(who == ME),
                "sent_at": int(when.timestamp()),
                "body": body,
                "kind": kind,
                **extra,
            }
        )
        return mid

    def transcript(self, conv: str, mid: str, text: str, conf: float = 0.9) -> None:
        self.derived.append((conv, mid, "transcript", "en", conf, text, NOW_TS - 3600))

    def call(self, cid: str, peer: str, when: datetime, outcome: str, outgoing: bool = False) -> None:
        start = int(when.timestamp())
        self.calls.append((cid, peer, None, int(outgoing), 0, start, start + 60, outcome))

    def schedule(self, target: str, body: str, when: datetime) -> None:
        self.scheduled.append((target, body, int(when.timestamp()), "pending", NOW_TS - 86400))

    def write(self, root: Path) -> Path:
        store = root / "store"
        store.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(store / "wa.db")
        for i, sql in enumerate(synth.go_migrations(), start=1):
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(i),),
            )
        last: dict[str, int] = {}
        for m in self.msgs:
            last[m["conv"]] = max(last.get(m["conv"], 0), m["sent_at"])
        conn.executemany(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            [(c, TITLES.get(c, ""), int(c == GROUP), t) for c, t in last.items()],
        )
        for m in self.msgs:
            conn.execute(
                "INSERT INTO chat_messages (conv, msg_id, author, outgoing, sent_at, body, kind, attachment_name, "
                "mime, byte_size, local_path, reply_to_id, reply_to_author, reply_preview, mentioned, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    m["conv"], m["msg_id"], m["author"], m["outgoing"], m["sent_at"], m["body"], m["kind"],
                    m.get("attachment_name"), m.get("mime"), m.get("byte_size"), m.get("local_path"),
                    m.get("reply_to_id"), m.get("reply_to_author"), m.get("reply_preview"),
                    json.dumps(m["mentioned"]) if m.get("mentioned") else None, m.get("read_at"),
                ),
            )  # fmt: skip
        conn.executemany("INSERT INTO derived_text VALUES (?, ?, ?, ?, ?, ?, ?)", self.derived)
        conn.executemany(
            "INSERT INTO call_log (call_id,peer,group_jid,outgoing,video,started_at,ended_at,outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            self.calls,
        )
        conn.executemany(
            "INSERT INTO scheduled_messages (target, body, next_run, status, created_at) VALUES (?, ?, ?, ?, ?)",
            self.scheduled,
        )
        conn.commit()
        conn.close()
        synth.build_session_db(store / "session.db")
        return store


IDS: dict[str, str] = {}


def two_weeks() -> Builder:
    b = Builder()
    lat = [2, 15, 90, 300]
    for d in range(10):
        laugh = " haha" if d % 3 == 0 else ""
        face = f" {GRIN}" if d % 2 == 0 else ""
        a = b.add(ALICE, ALICE, at(d, 9, d), f"Morning, I finished chapter {d + 1}{laugh}{face}")
        b.msgs[-1]["read_at"] = int(at(d, 9, d + 1).timestamp())
        b.add(
            ALICE,
            ME,
            at(d, 9, d + lat[d % 4]),
            "Nice, see you at the library",
            reply_to_id=a if d == 4 else None,
        )
        if d % 2 == 1:
            b.add(ALICE, ME, at(d, 15), "Did you like the ending?")
            b.add(ALICE, ALICE, at(d, 15, 10), f"Yes, loved it {TEARS}")
    b.add(ALICE, ALICE, at(3, 9, 30), "", "sticker", attachment_name="wave.webp", mime="image/webp")
    b.add(ALICE, ME, at(5, 10), "", "sticker", attachment_name="thumbs.webp", mime="image/webp")
    for d, text in (
        (2, "I will bring the snacks on Saturday"),
        (6, "Running late, start without me"),
        (8, ""),
    ):
        mid = b.add(ALICE, ALICE, at(d, 18), "", "voice", mime="audio/ogg", byte_size=24000)
        if text:
            b.transcript(ALICE, mid, text)
    b.add(ALICE, ME, at(7, 20), "", "voice", mime="audio/ogg", byte_size=16000)
    b.add(ALICE, ALICE, at(8, 2), "Cannot sleep, thinking about the plot")
    b.add(ALICE, ME, at(8, 9, 30), "Same here, the twist was great")
    IDS["cover"] = b.add(ALICE, ALICE, at(9, 11), "", "image", attachment_name="cover.jpg", mime="image/jpeg")
    IDS["cover_words"] = b.add(ALICE, ALICE, at(9, 11, 1), "this one looks better for the poster")

    # report: new -> in_progress -> follow-up -> done -> thanks -> fix by reply -> new
    b.call("CA1", ALICE, at(10, 9, 30), "missed")
    IDS["report"] = b.add(ALICE, ALICE, at(10, 10), "Can you please send me the updated report by Friday?")
    IDS["report_ok"] = b.add(ALICE, ME, at(10, 10, 20), "Sure, will do")
    IDS["report_ping"] = b.add(ALICE, ALICE, at(11, 9), "Any update on the report?")
    IDS["report_file"] = b.add(
        ALICE, ME, at(11, 11), "", "document", attachment_name="report.pdf", mime="application/pdf"
    )
    IDS["report_thanks"] = b.add(ALICE, ALICE, at(11, 11, 5), "Thanks, perfect")
    IDS["promise_today"] = b.add(ALICE, ME, at(11, 18), "I will send the invoice on Monday")
    IDS["report_fix"] = b.add(
        ALICE,
        ALICE,
        at(12, 9),
        "The report file is broken, the charts are missing",
        reply_to_id=IDS["report_file"],
    )
    IDS["logo"] = b.add(ALICE, ALICE, at(13, 14), "The logo on the website looks broken, please fix it")

    # Bob: overdue promise, a burst, waiting and answer, a missed call, a question at the end
    b.add(BOB, BOB, at(7, 10), "Hi, how are you?")
    b.add(BOB, ME, at(7, 10, 5), "Good, thanks")
    IDS["promise_overdue"] = b.add(BOB, ME, at(8, 15), "I'll send you the contract on Friday")
    IDS["banner"] = b.add(BOB, BOB, at(12, 10), "I need a new banner")
    b.add(BOB, BOB, at(12, 10, 1), "for the website")
    b.add(BOB, BOB, at(12, 10, 2), "asap please")
    IDS["flyer"] = b.add(BOB, BOB, at(12, 10, 15), "and also the flyer for the event")
    IDS["banner_q"] = b.add(BOB, ME, at(12, 10, 30), "Which size do you need?")
    IDS["banner_answer"] = b.add(BOB, BOB, at(12, 11), "1200 by 400 pixels")
    b.call("CB1", BOB, at(14, 8, 30), "missed")
    IDS["bob_last"] = b.add(BOB, BOB, at(14, 9), "Did you get my email? Can you check it?")

    # Carol: missed call returned by a call, last word is a thank-you
    b.add(CAROL_LID, CAROL_LID, at(10, 12), "Are you coming to the meeting on Thursday?")
    b.add(CAROL_LID, ME, at(10, 12, 5), "Yes")
    b.call("CC1", CAROL_LID, at(13, 11), "missed")
    b.call("CC2", CAROL_LID, at(13, 11, 30), "answered", outgoing=True)
    b.add(CAROL_LID, CAROL_LID, at(13, 12), "ok thanks")

    # Book club group
    for d in range(0, 10, 2):
        b.add(GROUP, ALICE, at(d, 19), f"Chapter {d + 1} was a page turner")
        b.add(GROUP, BOB, at(d, 19, 3), "Agreed, the ending surprised me")
        b.add(GROUP, CAROL_LID, at(d, 19, 6), "", "sticker", attachment_name="clap.webp", mime="image/webp")
        if d % 4 == 0:
            b.add(GROUP, ME, at(d, 19, 10), "Next one is my pick")
    b.add(GROUP, CAROL_LID, at(9, 18), "Meet at the library on Thursday")
    IDS["room"] = b.add(
        GROUP, CAROL_LID, at(11, 19), "Can you book the room for Thursday please", mentioned=[ME]
    )
    IDS["handouts"] = b.add(GROUP, BOB, at(11, 19, 5), "Someone should print the handouts please")
    IDS["room_done"] = b.add(GROUP, ME, at(11, 20), "Room is booked for Thursday, done")
    b.schedule(GROUP, "Reading tonight at 7", at(14, 18))
    b.schedule(GROUP, "Reminder for tomorrow", at(15, 18))
    return b


CONFIG: dict[str, Any] = {
    "tasks": {
        "projects": [{"id": "club-site", "name": "Book club site", "keywords": ["website", "logo", "poster"]}]
    },
    "mentions": {"watch": ["updated report", {"label": "Library", "words": ["library"]}]},
}
