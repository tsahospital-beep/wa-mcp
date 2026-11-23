"""Light task tracker in insight.db, rebuilt from the last N days of chats by refresh().

A task starts from a NEW_TASK or FIX burst. Later messages attach to it as steps (follow-ups, fixes,
voice notes, my replies) and move its status:
  new -> in_progress   I accepted ("ok", "on it")
  -> waiting           I asked the requester something
  waiting -> in_progress  the requester answered
  -> done              I sent a file, photo or link, or wrote "done" / "sent"
  done -> new          the requester complained again
  -> cancelled         the requester said "never mind"
  -> stale             open with no activity for tasks.stale_days (default 60); not shown in my_day
Tasks created before the window are kept as they are, except open ones younger than tasks.stale_days,
which are rebuilt from their first message. Rows are keyed by person (people.same_person), so a new
LID to phone mapping moves old rows to the new key. Reasons (status_why, step why) and next steps use
the fixed wording in WHY and NEXT_STEP only, never message text or matched words; message text is
copied only when wa.db is not encrypted.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wa_mcp import db, people
from wa_mcp.insight import base, pairing, projects, state, tasks, when
from wa_mcp.insight.base import Msg, Settings, Target

STATUSES = ("new", "in_progress", "waiting", "done", "cancelled", "stale")
OPEN = ("new", "in_progress", "waiting")
MAX_DAYS = 3650
FRESH_SECONDS = 3600
# Every stored reason comes from here; a category code (NEW_TASK, FIX) may follow in brackets.
WHY = {
    "request": "new request",
    "fix": "new complaint",
    "fix_after_done": "new complaint after delivery",
    "follow_up": "asked for status",
    "cancel": "the requester cancelled",
    "detail": "more detail from the requester",
    "material": "material from the requester",
    "answer": "the requester answered your question",
    "thanks": "the requester thanked you",
    "sent_link": "you sent a link",
    "sent_media": "you sent a file or media",
    "said_done": "you wrote that it is done",
    "accepted": "you accepted",
    "question": "you asked the requester a question",
}
NEXT_STEP = {
    "new": "Reply: confirm the request or ask for details",
    "in_progress": "Deliver the requested work",
    "waiting": "Wait for the requester to answer your question",
}


@dataclass
class Step:
    conv: str
    msg_id: str
    at: int
    who: str
    kind: str
    text: str
    why: str


@dataclass
class Task:
    id: str
    tg: Target
    asker: str
    asker_name: str
    category: str
    score: int
    title: str
    status: str
    status_why: str
    project: dict[str, Any] | None
    first: Msg
    created_at: int
    updated_at: int
    due: when.Due | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def msg_ids(self) -> set[str]:
        return {st.msg_id for st in self.steps}

    @property
    def pings(self) -> int:
        return sum(1 for st in self.steps if st.kind == "follow_up")

    def add(self, m: Msg, kind: str, why: str, label: str | None = None) -> None:
        if m.id in self.msg_ids:
            return
        text = (
            label
            if label is not None
            else (m.text or f"[{m.kind}{': ' + m.file_name if m.file_name else ''}]")
        )
        self.steps.append(Step(m.conv, m.id, m.ts, m.who, kind, text, why))
        self.updated_at = max(self.updated_at, m.ts)

    def set(self, status: str, why: str) -> None:
        self.status, self.status_why = status, why

    def next_step(self) -> str:
        """Fixed wording only: never message text or names."""
        if self.status not in NEXT_STEP:
            return ""
        pinged = f"The requester asked for status {self.pings} time(s). " if self.pings else ""
        due = f" (due {_stamp(self.due)})" if self.due and self.status != "waiting" else ""
        return pinged + NEXT_STEP[self.status] + due


def _stamp(due: when.Due) -> str:
    return due.at.strftime("%Y-%m-%d") if due.all_day else due.at.strftime("%Y-%m-%d %H:%M")


def task_id(conv: str, msg_id: str) -> str:
    return "t" + hashlib.sha1(f"{conv}|{msg_id}".encode(), usedforsecurity=False).hexdigest()[:12]


def _title(burst: list[Msg]) -> str:
    for m in burst:
        if m.text:
            return base.clip(m.text.splitlines()[0], 90)
    m = burst[0]
    return f"[{m.kind}{': ' + m.file_name if m.file_name else ''}]"


class _ChatRun:
    """Rebuilds the tasks of one chat from its messages, oldest first."""

    def __init__(self, tg: Target, s: Settings, names: base.Names, lex: when.Lexicon) -> None:
        self.tg, self.s, self.names, self.lex = tg, s, names, lex
        self.tasks: list[Task] = []

    def _open(self, asker: str | None = None) -> list[Task]:
        return [t for t in self.tasks if t.status in OPEN and (asker is None or t.asker == asker)]

    def _by_msg(self, msg_id: str) -> Task | None:
        if not msg_id:
            return None
        return next((t for t in reversed(self.tasks) if msg_id in t.msg_ids), None)

    def _latest(self, items: list[Task]) -> Task | None:
        return max(items, key=lambda t: t.updated_at) if items else None

    def _reply_target(self, msgs: list[Msg]) -> Task | None:
        return next((t for m in msgs if (t := self._by_msg(m.reply_to)) is not None), None)

    def _create(self, d: tasks.Detection) -> Task:
        first = d.first
        t = Task(
            id=task_id(first.conv, first.id),
            tg=self.tg,
            asker=first.who,
            asker_name=self.names(first.who),
            category=d.verdict.category,
            score=d.verdict.score,
            title=_title(d.burst),
            status="new",
            status_why=f"{WHY['request']} ({d.verdict.category})",
            project=d.project if d.project and d.project.get("id") else None,
            first=first,
            created_at=first.ts,
            updated_at=first.ts,
            due=when.parse_due(d.text, first.dt, self.lex),
        )
        for m in d.burst:
            t.add(m, "request", f"{WHY['request']} ({d.verdict.category})")
        self.tasks.append(t)
        return t

    def on_detection(self, d: tasks.Detection) -> None:
        cat, asker, s = d.verdict.category, d.first.who, self.s
        if cat == "TEAM":
            return
        if cat != "CANCEL":
            waiting = [t for t in self._open(asker) if t.status == "waiting"]
            w = self._latest(waiting)
            if w is not None:
                for m in d.burst:
                    w.add(m, "answer", WHY["answer"])
                w.set("in_progress", WHY["answer"])
        target = self._reply_target(d.burst)
        strong = d.verdict.score >= s.min_task_score
        if cat == "NEW_TASK" and strong:
            self._create(d)
        elif cat == "FIX":
            t = target or self._same_project(d) or self._recent(asker, d.first.ts)
            if t is None:
                if strong:
                    self._create(d)
                return
            for m in d.burst:
                t.add(m, "fix", WHY["fix"])
            if t.status in ("done", "cancelled"):
                t.set("new", WHY["fix_after_done"])
            elif t.status == "waiting":
                t.set("in_progress", WHY["fix"])
        elif cat == "FOLLOW_UP":
            t = (
                target
                or self._latest(self._open(asker))
                or (None if self.tg.is_group else self._latest(self._open()))
            )
            if t is not None:
                for m in d.burst:
                    t.add(m, "follow_up", WHY["follow_up"])
        elif cat == "CANCEL":
            t = target or self._latest(self._open(asker))
            if t is not None:
                for m in d.burst:
                    t.add(m, "cancel", WHY["cancel"])
                t.set("cancelled", WHY["cancel"])
        elif cat in ("VOICE", "PRECURSOR"):
            t = self._latest(self._open(asker))
            if t is not None and d.first.ts - t.updated_at <= 1800:
                kind = "detail" if cat == "VOICE" else "material"
                for m in d.burst:
                    t.add(m, kind, WHY[kind])
        elif cat == "CHAT" and s.task["thanks"].any(d.text):
            done = [t for t in self.tasks if t.asker == asker and t.status == "done"]
            t = target or self._latest(done)
            if t is not None and t.status == "done" and d.first.ts - t.updated_at <= 48 * 3600:
                for m in d.burst:
                    t.add(m, "thanks", WHY["thanks"])
                t.status_why += "; " + WHY["thanks"]

    def _same_project(self, d: tasks.Detection) -> Task | None:
        pid = (d.project or {}).get("id")
        if not pid:
            return None
        same = [
            t
            for t in self.tasks
            if (t.project or {}).get("id") == pid and d.first.ts - t.updated_at <= 14 * 86400
        ]
        return self._latest(same)

    def _recent(self, asker: str, ts: int) -> Task | None:
        recent = [t for t in self.tasks if t.asker == asker and ts - t.updated_at <= 12 * 3600]
        return self._latest(recent)

    def on_mine(self, m: Msg) -> None:
        s = self.s
        t = self._by_msg(m.reply_to) or self._latest(self._open())
        if t is None:
            return
        done = base.said_done(m.text, s)
        if m.kind in pairing.PAIRED_KINDS or m.has_link or done:
            if m.has_link and m.kind == "text":
                why = WHY["sent_link"]
            else:
                why = WHY["sent_media"] if m.kind != "text" else WHY["said_done"]
            t.add(m, "delivery", why)
            t.set("done", why)
        elif s.task["progress"].any(m.text) or s.task["accept"].any(m.text):
            if t.status == "new":
                t.add(m, "accepted", WHY["accepted"])
                t.set("in_progress", WHY["accepted"])
            if t.due is None:
                t.due = when.parse_due(m.text, m.dt, self.lex)
        elif (s.task["waiting"].any(m.text) or "?" in m.plain) and t.status in ("new", "in_progress"):
            t.add(m, "question", WHY["question"])
            t.set("waiting", WHY["question"])

    def build(self, msgs: list[Msg]) -> list[Task]:
        dets = tasks.detect(msgs, self.tg, self.s)
        pos = {id(m): i for i, m in enumerate(msgs)}
        events: list[tuple[int, int, Msg | tasks.Detection]] = [(pos[id(d.last)], 1, d) for d in dets]
        events += [(pos[id(m)], 0, m) for m in msgs if m.me]
        for _, _, ev in sorted(events, key=lambda e: (e[0], e[1])):
            if isinstance(ev, tasks.Detection):
                self.on_detection(ev)
            else:
                self.on_mine(ev)
        return self.tasks


def _save(conn: sqlite3.Connection, tg: Target, since: int, built: list[Task]) -> None:
    marks = ", ".join("?" * len(tg.jids))
    old = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM tasks WHERE chat IN ({marks}) AND created_at >= ?", (*tg.jids, since)
        )
    ]
    for start in range(0, len(old), 500):
        chunk = old[start : start + 500]
        marks = ", ".join("?" * len(chunk))
        conn.execute(f"DELETE FROM task_steps WHERE task_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM tasks WHERE id IN ({marks})", chunk)
    for t in built:
        project = t.project or {}
        conn.execute(
            "INSERT OR REPLACE INTO tasks (id, chat, chat_name, is_group, asker, asker_name, category, score, title, "
            "status, status_why, project, project_name, next_step, due_at, due_hint, first_conv, first_msg, "
            "created_at, updated_at, pings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.id, tg.key, tg.name, int(tg.is_group), t.asker, t.asker_name, t.category, t.score,
                state.stored_text(t.title, 120), t.status, t.status_why, project.get("id"), project.get("name"),
                t.next_step(), _due_ts(t.due),
                (state.stored_text(t.due.hint(), 80) or None) if t.due else None,
                t.first.conv, t.first.id, t.created_at, t.updated_at, t.pings,
            ),
        )  # fmt: skip
        conn.executemany(
            "INSERT OR REPLACE INTO task_steps (task_id, conv, msg_id, at, who, kind, text, why) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (t.id, st.conv, st.msg_id, st.at, st.who, st.kind, state.stored_text(st.text), st.why)
                for st in t.steps
            ],
        )


def _fingerprint(since: int, days: int, s: Settings) -> str:
    chats = db.active_chats(after=since)
    count = sum(int(c.get("message_count") or 0) for c in chats)
    last = max((int(c["last_activity"] or 0) for c in chats), default=0)
    return f"{days}:{count}:{last}:{s.fingerprint}"


def _rekey(conn: sqlite3.Connection) -> int:
    """Move rows to the current key of each person (the phone form once a LID mapping is known)."""
    changed = 0
    for table, col in (("tasks", "chat"), ("tasks", "asker"), ("task_steps", "who")):
        for (value,) in conn.execute(f"SELECT DISTINCT {col} FROM {table}").fetchall():
            if value == base.ME or "@" not in str(value):
                continue
            key = people.same_person(str(value))[0]
            if key != value:
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (key, value))
                changed += 1
    return changed + state.rekey_history(conn, lambda conv: people.same_person(conv)[0])


def _age_out(conn: sqlite3.Connection, now_ts: int, s: Settings) -> int:
    """Open tasks with no activity for stale_days become stale; closed ones older than keep_days are removed."""
    cur = conn.execute(
        "UPDATE tasks SET status = 'stale', status_why = ?, next_step = '' "
        "WHERE status IN ('new', 'in_progress', 'waiting') AND updated_at < ?",
        (f"no activity for more than {s.stale_days} days", now_ts - s.stale_days * 86400),
    )
    cutoff = now_ts - s.keep_days * 86400
    old = "SELECT id FROM tasks WHERE status IN ('done', 'cancelled', 'stale') AND updated_at < ?"
    conn.execute(f"DELETE FROM task_steps WHERE task_id IN ({old})", (cutoff,))
    conn.execute(
        "DELETE FROM tasks WHERE status IN ('done', 'cancelled', 'stale') AND updated_at < ?", (cutoff,)
    )
    return int(cur.rowcount)


def refresh(days: int | None = None, now: float | None = None) -> dict[str, Any]:
    """Rebuild the tracker from the last `days` days (default tasks.refresh_days)."""
    s = base.settings()
    days = int(days or s.refresh_days)
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    now_ts = int(time.time() if now is None else now)
    start = now_ts - days * 86400
    lex = when.lexicon()
    names = base.Names()
    with state.connect() as conn:
        with state.transaction(conn):
            _rekey(conn)
        rows = conn.execute(
            "SELECT chat, MIN(created_at) FROM tasks WHERE status IN ('new', 'in_progress', 'waiting') "
            "AND created_at < ? AND created_at >= ? GROUP BY chat",
            (start, now_ts - s.stale_days * 86400),
        ).fetchall()
    old_open = {str(r[0]): int(r[1]) for r in rows}
    targets = base.chat_targets(after=start, before=now_ts + 1)
    known = {t.key for t in targets}
    for chat in old_open:
        if chat not in known:
            jids = tuple(people.same_person(chat))
            targets.append(Target(jids, people.name_of(chat), people.server_of(chat) == people.GROUP_SERVER))
    built_total, open_total = 0, 0
    with state.connect() as conn:
        for tg in targets:
            since = min(start, old_open.get(tg.key, start))
            msgs = base.load(tg.jids, after=since - 1, before=now_ts + 1, imported=s.include_imported)
            built = _ChatRun(tg, s, names, lex).build(msgs)
            for t in built:
                if t.status in OPEN and now_ts - t.updated_at > s.stale_days * 86400:
                    t.set("stale", f"no activity for more than {s.stale_days} days")
            with state.transaction(conn):
                _save(conn, tg, since, built)
            built_total += len(built)
            open_total += sum(1 for t in built if t.status in OPEN)
        with state.transaction(conn):
            _age_out(conn, now_ts, s)
            state.prune_cache(conn, s.cache_days, 200, now=now_ts)
            state.set_meta(conn, "tasks_refreshed_at", str(now_ts))
            state.set_meta(conn, "tasks_days", str(days))
            state.set_meta(conn, "tasks_fingerprint", _fingerprint(start, days, s))
    return {
        "chats": len(targets),
        "tasks": built_total,
        "open": open_total,
        "since": db.iso_local(start),
        "refreshed_at": db.iso_local(now_ts),
    }


def ensure_fresh(now: float | None = None) -> bool:
    """Refresh when messages changed since the last refresh or it is older than an hour. True if refreshed."""
    s = base.settings()
    now_ts = int(time.time() if now is None else now)
    with state.connect() as conn:
        at = int(state.get_meta(conn, "tasks_refreshed_at") or 0)
        days = int(state.get_meta(conn, "tasks_days") or s.refresh_days)
        fp = state.get_meta(conn, "tasks_fingerprint")
    if at and now_ts - at < FRESH_SECONDS and fp == _fingerprint(now_ts - days * 86400, days, s):
        return False
    refresh(days, now=now_ts)
    return True


# reading


def _message_text(conv: str, msg_id: str) -> str:
    row = db.message(conv, msg_id)
    if row is None:
        return ""
    return str(row["body"] or "") or f"[{row['kind']}]"


def shape_row(r: sqlite3.Row) -> dict[str, Any]:
    title = r["title"] or base.clip(_message_text(r["first_conv"], r["first_msg"]), 90)
    return {
        "id": r["id"],
        "title": title,
        "status": r["status"],
        "status_why": r["status_why"],
        "category": r["category"],
        "score": r["score"],
        "chat": r["chat"],
        "chat_name": r["chat_name"],
        "from": r["asker_name"],
        "from_jid": r["asker"],
        "project": {"id": r["project"], "name": r["project_name"]} if r["project"] else None,
        "next_step": r["next_step"],
        "due": r["due_hint"] or (f"due {_due_stamp(r['due_at'])}" if r["due_at"] else None),
        "created": db.iso_local(r["created_at"]),
        "updated": db.iso_local(r["updated_at"]),
        "pings": r["pings"],
        "first_message": {"chat": r["first_conv"], "id": r["first_msg"]},
    }


def _due_ts(due: when.Due | None) -> int | None:
    """Unix seconds of a due moment; None when the platform cannot represent it."""
    if due is None:
        return None
    try:
        return int(due.end().timestamp())
    except (OSError, OverflowError, ValueError):
        return None


def _due_stamp(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "an unknown date"


def _order(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    rank = {s: i for i, s in enumerate(STATUSES)}
    return sorted(rows, key=lambda r: (rank.get(r["status"], 9), -int(r["pings"]), -int(r["updated_at"])))


def overview(
    status: str | None = None,
    project: str | None = None,
    chat: list[str] | None = None,
    query: str | None = None,
    open_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    where, args = ["1 = 1"], []
    if status:
        where.append("status = ?")
        args.append(status)
    elif open_only:
        where.append("status IN ('new', 'in_progress', 'waiting')")
    if chat:
        where.append(f"chat IN ({', '.join('?' * len(chat))})")
        args += chat
    if project:
        where.append("(project = ? OR lower(project_name) LIKE ?)")
        args += [project, f"%{project.casefold()}%"]
    with state.connect() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE " + " AND ".join(where), args).fetchall()
        refreshed = state.get_meta(conn, "tasks_refreshed_at")
    items = [shape_row(r) for r in _order(rows)]
    if query:
        q = query.casefold()
        items = [
            t
            for t in items
            if q in " ".join(str(t[k] or "") for k in ("title", "chat_name", "from")).casefold()
        ]
    counts: dict[str, int] = {}
    for t in items:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "refreshed_at": db.iso_local(int(refreshed)) if refreshed else None,
        "count": len(items),
        "by_status": counts,
        "tasks": items[offset : offset + limit],
        "next_offset": offset + len(items[offset : offset + limit]),
    }


def _steps(conn: sqlite3.Connection, task: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM task_steps WHERE task_id = ? ORDER BY at, msg_id", (task,)).fetchall()
    return [
        {
            "time": db.iso_local(r["at"]),
            "chat": r["conv"],
            "message_id": r["msg_id"],
            "who": "me" if r["who"] == base.ME else people.name_of(r["who"]),
            "kind": r["kind"],
            "text": r["text"] or _message_text(r["conv"], r["msg_id"]),
            "why": r["why"],
        }
        for r in rows
    ]


def detail(task: str | None = None, query: str | None = None) -> dict[str, Any]:
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task,)).fetchone() if task else None
        if row is None and query:
            found = overview(query=query, open_only=False, limit=200)["tasks"]
            if found:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (found[0]["id"],)).fetchone()
        if row is None:
            return {"found": False}
        return {"found": True, "task": shape_row(row), "steps": _steps(conn, row["id"])}


def which(message_id: str, convs: list[str] | None = None) -> dict[str, Any]:
    with state.connect() as conn:
        rows = conn.execute("SELECT * FROM task_steps WHERE msg_id = ?", (message_id,)).fetchall()
        rows = [r for r in rows if not convs or r["conv"] in convs]
        if not rows:
            return {
                "found": False,
                "note": "the message is not part of any tracked task (small talk, or not refreshed)",
            }
        step = rows[0]
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (step["task_id"],)).fetchone()
    return {
        "found": True,
        "task": shape_row(task),
        "step": {"time": db.iso_local(step["at"]), "kind": step["kind"], "why": step["why"]},
    }


def project_overview(query: str) -> dict[str, Any]:
    p = projects.find(query, base.settings().projects)
    with state.connect() as conn:
        if p is not None:
            rows = conn.execute("SELECT * FROM tasks WHERE project = ?", (p.id,)).fetchall()
        else:
            like = f"%{query.casefold()}%"
            rows = conn.execute(
                "SELECT * FROM tasks WHERE lower(project_name) LIKE ? OR lower(project) LIKE ?", (like, like)
            ).fetchall()
    items = [shape_row(r) for r in _order(rows)]
    counts: dict[str, int] = {}
    asked: dict[str, int] = {}
    for t in items:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        asked[t["from"]] = asked.get(t["from"], 0) + 1
    return {
        "project": {"id": p.id, "name": p.name, "keywords": list(p.keywords)} if p else {"query": query},
        "configured": p is not None,
        "tasks": len(items),
        "by_status": counts,
        "open": [t for t in items if t["status"] in OPEN][:50],
        "recent_closed": [t for t in items if t["status"] not in OPEN][:10],
        "people": sorted(asked.items(), key=lambda kv: -kv[1])[:10],
        "last_activity": max((t["updated"] for t in items if t["updated"]), default=None),
    }


def whats_new(hours: float = 24, now: float | None = None, limit: int = 100) -> dict[str, Any]:
    if not 0 < hours <= MAX_DAYS * 24:
        raise ValueError(f"hours must be between 1 and {MAX_DAYS * 24}")
    now_ts = int(time.time() if now is None else now)
    since = now_ts - int(hours * 3600)
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT s.*, t.title, t.status, t.chat_name, t.first_conv, t.first_msg, t.created_at "
            "FROM task_steps s JOIN tasks t ON t.id = s.task_id WHERE s.at >= ? AND s.at <= ? ORDER BY s.at DESC",
            (since, now_ts),
        ).fetchall()
        created = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at <= ?", (since, now_ts)
        ).fetchone()[0]
    steps = []
    for r in rows[:limit]:
        steps.append(
            {
                "time": db.iso_local(r["at"]),
                "task_id": r["task_id"],
                "task": r["title"] or base.clip(_message_text(r["first_conv"], r["first_msg"]), 90),
                "status": r["status"],
                "chat_name": r["chat_name"],
                "who": "me" if r["who"] == base.ME else people.name_of(r["who"]),
                "kind": r["kind"],
                "text": base.clip(r["text"] or _message_text(r["conv"], r["msg_id"]), 200),
                "why": r["why"],
                "message_id": r["msg_id"],
            }
        )
    return {
        "since": db.iso_local(since),
        "new_tasks": created,
        "steps": steps,
        "count": len(rows),
        "as_of": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
    }
