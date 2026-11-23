"""A chat prepared for analysis: turns, sessions, reply pairs and caches shared by the sections.

"me" is the account owner, "them" the other side (every other participant in a group).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wa_mcp import people
from wa_mcp.insight import base, pairing, tasks
from wa_mcp.insight.base import ME, THEM, Msg, Settings, Target

Data = dict[str, Any]
SIDES = (ME, THEM)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BUCKETS = (
    ("under_5m", 5.0),
    ("under_1h", 60.0),
    ("under_3h", 180.0),
    ("under_24h", 1440.0),
    ("over_24h", 1e12),
)


@dataclass
class Turn:
    i: int
    who: str
    side: str
    msgs: list[Msg]
    sess: int = 0

    @property
    def start(self) -> int:
        return self.msgs[0].ts

    @property
    def end(self) -> int:
        return self.msgs[-1].ts

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.msgs if m.text)

    @property
    def kinds(self) -> list[str]:
        return [m.kind for m in self.msgs if m.kind != "text"]


@dataclass
class Session:
    start: int
    end: int
    who: str
    side: str
    turns: list[int] = field(default_factory=list)


@dataclass
class Pair:
    incoming: Turn
    reply: Turn | None
    minutes: float | None
    night: bool


class Chat:
    """One chat prepared for analysis: messages, turns, sessions and caches shared by sections."""

    def __init__(self, tg: Target, msgs: list[Msg], s: Settings, n_examples: int = 8) -> None:
        self.tg, self.msgs, self.s, self.n_ex = tg, msgs, s, n_examples
        self.names = base.Names()
        self.other = base.mask(tg.name) or "them"
        self.by_id = {m.id: m for m in msgs}
        self.my_ids = {m.id for m in msgs if m.me}
        self.my_jids = set(people.my_jids())
        self.turns = self._turns()
        self.sessions = self._sessions()
        self._pairs: list[Pair] | None = None
        self._theirs: list[float] = []
        self._dets: list[tasks.Detection] | None = None

    def label(self, who: str) -> str:
        if who == ME:
            return ME
        return self.names(who) if self.tg.is_group else self.other

    def _turns(self) -> list[Turn]:
        out: list[Turn] = []
        for m in self.msgs:
            key = m.who if self.tg.is_group else m.side
            last = out[-1] if out else None
            same = last is not None and (last.who if self.tg.is_group else last.side) == key
            if last is not None and same and m.ts - last.end <= self.s.turn_gap_seconds:
                last.msgs.append(m)
                continue
            out.append(Turn(len(out), m.who, m.side, [m]))
        return out

    def _sessions(self) -> list[Session]:
        out: list[Session] = []
        for t in self.turns:
            if not out or t.start - out[-1].end >= self.s.session_gap_hours * 3600:
                out.append(Session(t.start, t.end, t.who, t.side))
            out[-1].end = t.end
            out[-1].turns.append(t.i)
            t.sess = len(out) - 1
        return out

    def night(self, ts: int) -> bool:
        a, b = self.s.night
        h = datetime.fromtimestamp(ts).hour
        return a <= h < b if a <= b else (h >= a or h < b)

    def aimed(self, t: Turn) -> bool:
        return any(base.addressed_to_me(m, self.my_ids, self.my_jids, self.s) for m in t.msgs)

    def pairs(self) -> list[Pair]:
        """Incoming turn -> my reply (groups: only turns aimed at me) and my turn -> their reply (direct chats)."""
        if self._pairs is not None:
            return self._pairs
        out, theirs = [], []
        tr = self.turns
        for i, t in enumerate(tr):
            nxt = tr[i + 1] if i + 1 < len(tr) else None
            if t.side == THEM:
                if self.tg.is_group:
                    if not self.aimed(t):
                        continue
                    rep = next((x for x in tr[i + 1 : i + 40] if x.side == ME), None)
                else:
                    if nxt is not None and nxt.side == THEM and nxt.start - t.end < 6 * 3600:
                        continue
                    rep = nxt if nxt is not None and nxt.side == ME else None
                lat = (rep.start - t.end) / 60 if rep is not None else None
                out.append(Pair(t, rep, lat, self.night(t.end) and (lat is None or lat > 5)))
            elif t.side == ME and nxt is not None and nxt.side == THEM and not self.tg.is_group:
                theirs.append((nxt.start - t.end) / 60)
        self._pairs, self._theirs = out, theirs
        return out

    def detections(self) -> list[tasks.Detection]:
        if self._dets is None:
            self._dets = tasks.detect(self.msgs, self.tg, self.s)
        return self._dets

    def ex(self, m: Msg, text: str | None = None, **extra: Any) -> dict[str, Any] | None:
        return base.example(m, self.label(m.who), text, **extra)

    def substantive_after(self, ts: int, hours: float = 72) -> bool:
        for m in self.msgs:
            if m.ts <= ts or not m.me:
                continue
            if m.ts > ts + hours * 3600:
                break
            if (
                m.kind in pairing.PAIRED_KINDS
                or m.has_link
                or len(m.plain.split()) >= 5
                or base.said_done(m.text, self.s)
            ):
                return True
        return False


def sec(n: int, data: Data, examples: list[dict[str, Any] | None] | None = None, low: int = 30) -> Data:
    return {"n": n, "low_data": n < low, "data": data, "examples": [e for e in examples or [] if e]}
