"""Guess which project a request belongs to, from config tasks.projects = [{id, name, keywords}].

Optional per project: "chats" (JIDs that belong to this project only), "weak" (keywords common
outside the project, weigh less) and "generic": true (a catch-all bucket that only wins when
nothing specific matches). Longer keywords weigh more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wa_mcp.insight.base import Project, Settings

WEAK_MAX = 0.7
TOPIC_HOLD_SECONDS = 3 * 3600


@dataclass(frozen=True)
class Hit:
    project: str
    weight: float
    keyword: str


def hits(text: str, projects: tuple[Project, ...]) -> list[Hit]:
    """Best keyword hit per project, heaviest first."""
    low = " " + (text or "").casefold() + " "
    out = []
    for p in projects:
        best: Hit | None = None
        for k in p.keywords:
            found = re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low) if len(k) <= 4 else k in low
            if not found:
                continue
            w = min(1.0, 0.45 + len(k) / 20)
            if k in p.weak or len(k) < 6:
                w = min(w, WEAK_MAX)
            if p.generic:
                w *= 0.5
            if best is None or w > best.weight:
                best = Hit(p.id, round(w, 3), k)
        if best is not None:
            out.append(best)
    return sorted(out, key=lambda h: -h.weight)


def specific(found: list[Hit], projects: tuple[Project, ...]) -> list[Hit]:
    generic = {p.id for p in projects if p.generic}
    return [h for h in found if h.project not in generic]


def topic(text: str, projects: tuple[Project, ...]) -> str | None:
    """The one specific project a message talks about, or None (none or several)."""
    found = specific(hits(text, projects), projects)
    keys = [h.keyword for h in found]
    found = [h for h in found if not any(h.keyword != k and h.keyword in k for k in keys)]
    ids = {h.project for h in found}
    return found[0].project if len(ids) == 1 else None


def name_of(pid: str | None, projects: tuple[Project, ...]) -> str | None:
    if pid is None:
        return None
    return next((p.name for p in projects if p.id == pid), pid)


def find(query: str, projects: tuple[Project, ...]) -> Project | None:
    q = query.strip().casefold()
    exact = [p for p in projects if q in (p.id.casefold(), p.name.casefold())]
    if exact:
        return exact[0]
    return next((p for p in projects if q in p.id.casefold() or q in p.name.casefold()), None)


class Matcher:
    """Walks one chat in time order and assigns projects to requests, remembering the recent topic."""

    def __init__(self, chat_jids: tuple[str, ...], s: Settings) -> None:
        self.projects = s.projects
        owned = [p.id for p in self.projects if any(j in p.chats for j in chat_jids)]
        self.default = owned[0] if len(owned) == 1 else None
        self.last: tuple[str, int] | None = None

    def see(self, text: str, ts: int) -> None:
        """A message in the chat that sets the topic (my messages, or any message naming one project)."""
        pid = topic(text, self.projects)
        if pid is not None:
            self.last = (pid, ts)

    def _result(self, pid: str | None, confidence: int, why: str) -> dict[str, Any]:
        return {"id": pid, "name": name_of(pid, self.projects), "confidence": confidence, "why": why}

    def assign(self, text: str, quoted: str, ts: int, answer: str = "") -> dict[str, Any] | None:
        """{id, name, confidence 0-100, why}; None when no projects are configured."""
        if not self.projects:
            return None
        found = hits(text, self.projects)
        spec = specific(found, self.projects)
        if spec:
            head = specific(hits(text[:120], self.projects), self.projects)
            if head:
                spec = head + [h for h in spec if h.project != head[0].project]
            top = spec[0]
            clear = len(spec) == 1 or spec[0].weight - spec[1].weight > 0.1 or bool(head)
            self.last = (top.project, ts)
            return self._result(
                top.project, round(60 + 35 * top.weight) if clear else 55, f"keyword '{top.keyword}'"
            )
        q = specific(hits(quoted, self.projects), self.projects)
        if q:
            return self._result(q[0].project, 80, f"reply to a message about '{q[0].keyword}'")
        if self.default:
            return self._result(self.default, 75, "the chat belongs to this project")
        a = specific(hits(answer, self.projects), self.projects)
        if a and len({h.project for h in a}) == 1 and len(answer.strip()) <= 80:
            return self._result(a[0].project, 70, f"your answer mentions '{a[0].keyword}'")
        if found:
            return self._result(found[0].project, 55, f"generic keyword '{found[0].keyword}'")
        if self.last is not None and ts - self.last[1] <= TOPIC_HOLD_SECONDS:
            return self._result(self.last[0], 40, "last project discussed in this chat within 3 h")
        return self._result(None, 0, "no project signal")
