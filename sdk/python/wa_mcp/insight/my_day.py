"""What is on my plate for a day: open requests, my promises, people waiting for my answer,
missed calls and scheduled sends, grouped by person or group. No language model involved.
"""

from __future__ import annotations

import time as clock
from datetime import date, datetime
from typing import Any

from wa_mcp import db, people
from wa_mcp.insight import base, pairing, state, tracker, when
from wa_mcp.insight.base import Msg, Settings, Target

LOOKBACK_DAYS = 14
_RANK = {
    "promise_overdue": 0,
    "missed_call": 1,
    "task_due": 2,
    "question": 3,
    "task": 4,
    "promise_today": 5,
    "waiting_reply": 6,
    "scheduled": 7,
}


def _day(value: str | date | None, now: float) -> date:
    if value is None or value == "":
        return datetime.fromtimestamp(now).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"date {value!r}: use YYYY-MM-DD") from exc


def _delivered(m: Msg, s: Settings) -> bool:
    return m.kind in pairing.PAIRED_KINDS or m.has_link or bool(base.said_done(m.text, s))


def _promises(
    msgs: list[Msg], tg: Target, day: date, ref: float, s: Settings, lex: when.Lexicon
) -> list[dict[str, Any]]:
    out = []
    for i, m in enumerate(msgs):
        if not m.me or not s.words["promise"].any(m.text):
            continue
        due = when.parse_due(m.text, m.dt, lex)
        if due is None or due.day > day:
            continue
        later = [x for x in msgs[i + 1 :] if x.me and x.ts <= ref]
        if any(_delivered(x, s) for x in later):
            continue
        overdue = due.day < day or due.end() < datetime.fromtimestamp(ref)
        out.append(
            {
                "type": "promise",
                "rank": "promise_overdue" if overdue else "promise_today",
                "status": "overdue" if overdue else "due_today",
                "chat": m.conv,
                "message_id": m.id,
                "time": m.time,
                "due": due.hint(),
                "context": base.clip(m.text, 160),
            }
        )
    return out


def _waiting(
    msgs: list[Msg], tg: Target, ref: float, s: Settings, my_jids: set[str]
) -> dict[str, Any] | None:
    seen = [m for m in msgs if m.ts <= ref]
    last_mine = max((m.ts for m in seen if m.me), default=0)
    pending = [m for m in seen if not m.me and m.ts > last_mine]
    if not pending:
        return None
    if tg.is_group:
        my_ids = {m.id for m in seen if m.me}
        pending = [m for m in pending if base.addressed_to_me(m, my_ids, my_jids, s)]
        if not pending:
            return None
    text = "\n".join(m.text for m in pending if m.text)
    question = "?" in text or s.words["ask"].any(text) or s.task["request"].any(text)
    only_ack = (
        not question
        and all(m.kind in ("text", "sticker") for m in pending)
        and (not text or (len(base.URL_RE.sub("", text)) <= 25 and s.task["chat"].any(text)))
    )
    if only_ack:
        return None
    first = pending[0]
    return {
        "type": "waiting_reply",
        "rank": "question" if question else "waiting_reply",
        "chat": first.conv,
        "message_id": pending[-1].id,
        "time": first.time,
        "waiting_minutes": round((ref - first.ts) / 60),
        "question": question,
        "unanswered": len(pending),
        "context": base.clip(" / ".join(m.text or f"[{m.kind}]" for m in pending[-3:]), 200),
    }


def _missed_calls(since: float, ref: float, only: Target | None) -> list[tuple[Target, dict[str, Any]]]:
    calls = [c for c in db.calls(since=int(since), limit=None) if c["started_at"] <= ref]
    calls.sort(key=lambda c: int(c["started_at"]))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        peer = people.same_person(str(c["group_jid"] or c["peer"]))[0]
        grouped.setdefault(peer, []).append(c)
    out = []
    for peer, items in grouped.items():
        jids = tuple(people.same_person(peer))
        if only is not None and not set(jids) & set(only.jids):
            continue
        missed = [c for c in items if c["outcome"] == "missed" and not c["outgoing"]]
        if not missed:
            continue
        last_missed = int(missed[-1]["started_at"])
        answered = [
            c
            for c in items
            if int(c["started_at"]) > int(missed[0]["started_at"])
            and (c["outgoing"] or c["outcome"] == "answered")
        ]
        open_missed = [
            c for c in missed if not any(int(a["started_at"]) > int(c["started_at"]) for a in answered)
        ]
        if not open_missed:
            continue
        replied = db.messages(
            conv=list(jids), after=int(open_missed[0]["started_at"]), before=int(ref) + 1, limit=200
        )
        if any(r["outgoing"] for r in replied):
            continue
        tg = Target(jids, people.name_of(peer), people.server_of(peer) == people.GROUP_SERVER)
        first = open_missed[0]
        out.append(
            (
                tg,
                {
                    "type": "missed_call",
                    "rank": "missed_call",
                    "chat": peer,
                    "call_id": first["call_id"],
                    "message_id": None,
                    "time": db.iso_local(last_missed),
                    "missed": len(open_missed),
                    "video": any(bool(c["video"]) for c in open_missed),
                    "context": f"{len(open_missed)} missed {'video' if first['video'] else 'voice'} call(s), "
                    "not called back and no message since",
                },
            )
        )
    return out


def _scheduled(day: date, only: Target | None) -> list[tuple[Target, dict[str, Any]]]:
    start, end = when.day_bounds(day)
    out = []
    for r in db.scheduled(status="pending"):
        if not start <= int(r["next_run"]) < end:
            continue
        jids = tuple(people.same_person(str(r["target"])))
        if only is not None and not set(jids) & set(only.jids):
            continue
        tg = Target(jids, people.name_of(jids[0]), people.server_of(jids[0]) == people.GROUP_SERVER)
        out.append(
            (
                tg,
                {
                    "type": "scheduled",
                    "rank": "scheduled",
                    "chat": str(r["target"]),
                    "scheduled_id": r["id"],
                    "message_id": None,
                    "time": db.iso_local(int(r["next_run"])),
                    "recurrence": r["recurrence"],
                    "context": base.clip(
                        f"{r['kind']} {r['source_message_id']}: {r['body']}"
                        if r.get("source_chat")
                        else str(r["body"] or "[media]"),
                        160,
                    ),
                },
            )
        )
    return out


def _tasks(day: date, ref: float, only: Target | None) -> list[tuple[Target, dict[str, Any]]]:
    start, end = when.day_bounds(day)
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('new', 'in_progress', 'waiting') AND created_at <= ? "
            "ORDER BY updated_at DESC",
            (int(ref),),
        ).fetchall()
    out = []
    for r in rows:
        jids = tuple(people.same_person(str(r["chat"])))
        if only is not None and not set(jids) & set(only.jids):
            continue
        due_at = r["due_at"]
        due = (
            None
            if due_at is None
            else ("overdue" if due_at < start else "due_today" if due_at < end else "later")
        )
        shaped = tracker.shape_row(r)
        tg = Target(jids, str(r["chat_name"]), bool(r["is_group"]))
        out.append(
            (
                tg,
                {
                    "type": "task",
                    "rank": "task_due" if due in ("overdue", "due_today") else "task",
                    "task_id": r["id"],
                    "status": r["status"],
                    "chat": shaped["first_message"]["chat"],
                    "message_id": shaped["first_message"]["id"],
                    "time": shaped["created"],
                    "due": r["due_hint"],
                    "due_status": due,
                    "pings": r["pings"],
                    "next_step": r["next_step"],
                    "context": shaped["title"],
                },
            )
        )
    return out


def my_day(
    day: str | date | None = None,
    chat: str | None = None,
    now: float | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    refresh: bool = True,
) -> dict[str, Any]:
    """Everything that needs me on `day` (default today), grouped by person or group."""
    return _finish(*_collect(day, chat, now, lookback_days, refresh))


def my_day_accounts(
    day: str | date | None = None,
    chat: str | None = None,
    now: float | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    refresh: bool = True,
    accounts: list[str] | None = None,
) -> dict[str, Any]:
    """my_day over several accounts of the config (default: all), merged into one list. Every group and
    item has an `account` field. Accounts without a database yet, or without the given chat, are listed
    under `skipped`."""
    names = accounts or list(db.store().cfg.account_names)
    real_now = clock.time() if now is None else now
    merged: list[dict[str, Any]] = []
    used: list[str] = []
    skipped: list[dict[str, str]] = []
    plan: tuple[date, float] | None = None
    missing: LookupError | None = None
    for name in names:
        with db.use_account(name):
            try:
                d, ref, groups = _collect(day, chat, real_now, lookback_days, refresh)
            except db.StoreError as exc:
                skipped.append({"account": name, "reason": str(exc)})
                continue
            except LookupError as exc:
                if not chat or isinstance(exc, people.AmbiguousChat):
                    raise
                missing = missing or exc
                skipped.append({"account": name, "reason": str(exc)})
                continue
        plan = (d, ref)
        used.append(name)
        for g in groups:
            g["account"] = name
            for it in g["items"]:
                it["account"] = name
        merged += groups
    if plan is None:
        if missing is not None:
            raise missing
        raise db.StoreError("no account has a database yet: start the bridges once")
    out = _finish(plan[0], plan[1], merged)
    out["accounts"] = used
    if skipped:
        out["skipped"] = skipped
    return out


def _collect(
    day: str | date | None, chat: str | None, now: float | None, lookback_days: int, refresh: bool
) -> tuple[date, float, list[dict[str, Any]]]:
    """The groups of one account, items still carrying their rank."""
    if not 1 <= lookback_days <= 365:
        raise ValueError("lookback_days must be between 1 and 365")
    s = base.settings()
    lex = when.lexicon()
    real_now = clock.time() if now is None else now
    d = _day(day, real_now)
    ref = min(real_now, when.day_bounds(d)[1] - 1)
    since = ref - lookback_days * 86400
    only = base.target(chat) if chat else None
    if refresh:
        tracker.ensure_fresh(now=real_now)
    my_jids = set(people.my_jids())
    groups: dict[str, dict[str, Any]] = {}

    def put(tg: Target, item: dict[str, Any]) -> None:
        g = groups.setdefault(tg.key, {"chat": tg.key, "name": tg.name, "is_group": tg.is_group, "items": []})
        g["items"].append(item)

    targets = [only] if only is not None else base.chat_targets(after=since, before=ref + 1)
    for tg in targets:
        msgs = base.load(tg.jids, after=since, before=ref + 1, imported=s.include_imported)
        for item in _promises(msgs, tg, d, ref, s, lex):
            put(tg, item)
        waiting = _waiting(msgs, tg, ref, s, my_jids)
        if waiting is not None:
            put(tg, waiting)
    for tg, item in _tasks(d, ref, only) + _missed_calls(since, ref, only) + _scheduled(d, only):
        put(tg, item)
    return d, ref, list(groups.values())


def _finish(d: date, ref: float, groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Order the groups (most urgent first), count the items and drop the internal ranks."""
    for g in groups:
        g["items"].sort(
            key=lambda it: (_RANK[it["rank"]], -(it.get("waiting_minutes") or 0), str(it["time"]))
        )
        g["top"] = g["items"][0]["rank"]
    ordered = sorted(
        groups,
        key=lambda g: (_RANK[g["top"]], -max(it.get("waiting_minutes") or 0 for it in g["items"])),
    )
    items = [it for g in ordered for it in g["items"]]
    counts = {
        "open_tasks": sum(1 for it in items if it["type"] == "task"),
        "tasks_due": sum(1 for it in items if it["rank"] == "task_due"),
        "promises_due": sum(1 for it in items if it["type"] == "promise"),
        "promises_overdue": sum(1 for it in items if it["rank"] == "promise_overdue"),
        "waiting_for_reply": sum(1 for it in items if it["type"] == "waiting_reply"),
        "questions": sum(1 for it in items if it["rank"] == "question"),
        "missed_calls": sum(1 for it in items if it["type"] == "missed_call"),
        "scheduled": sum(1 for it in items if it["type"] == "scheduled"),
    }
    for it in items:
        it.pop("rank", None)
    for g in ordered:
        g.pop("top", None)
    return {
        "date": d.isoformat(),
        "as_of": datetime.fromtimestamp(ref).isoformat(timespec="seconds"),
        "summary": _summary(d, counts),
        "counts": counts,
        "people": ordered,
    }


def _summary(d: date, c: dict[str, int]) -> str:
    parts = [
        f"{c['open_tasks']} open request(s)" + (f", {c['tasks_due']} due" if c["tasks_due"] else ""),
        f"{c['promises_due']} promise(s) of yours due" + (f", {c['promises_overdue']} overdue" if c["promises_overdue"] else ""),
        f"{c['waiting_for_reply']} chat(s) waiting for your reply" + (f", {c['questions']} with a question" if c["questions"] else ""),
        f"{c['missed_calls']} missed call(s) not returned",
        f"{c['scheduled']} scheduled send(s)",
    ]  # fmt: skip
    return f"{d.isoformat()}: " + "; ".join(parts) + "."
