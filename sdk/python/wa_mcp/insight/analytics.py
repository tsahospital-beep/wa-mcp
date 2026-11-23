"""Chat analytics: how a conversation with one person (LID and phone chats merged) or one group works.

analyze() returns {"summary", "meta", "sections"}; every section is {n, low_data, data, examples}.
Results are cached in insight.db keyed by chat, period, sections and a fingerprint of the data
(message count, last message, derived text), so a new message or transcript invalidates them.
When wa.db is encrypted the cache stays in memory only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from statistics import median as stat_median
from typing import Any

from wa_mcp import db, fsutil, people
from wa_mcp.db import TimeArg, to_unix
from wa_mcp.insight import base, sections, state, style
from wa_mcp.insight.base import ME, THEM, fmt_minutes, num
from wa_mcp.insight.chat import Chat

VERSION = 1
DETAIL = {"brief": 1, "normal": 3, "full": 8}
_DROP = {
    "brief": {
        "by_month",
        "by_hour",
        "by_weekday",
        "by_time_of_day",
        "weekday_vs_weekend",
        "my_by_month",
        "untranscribed_ids",
    },
    "normal": {"by_hour", "my_by_month"},
    "full": set(),
}
_LIST_CAP = {"brief": 5, "normal": 10, "full": 1000}
_MEMORY: OrderedDict[str, dict[str, Any]] = OrderedDict()
_CACHE_ROWS = 200
BASELINE_ROWS = 20000
COMPARE_SECTIONS = (
    "volume",
    "reply_speed",
    "length",
    "emoji",
    "stickers",
    "voice",
    "deferrals",
    "formality",
    "script",
)


def _wanted(names: list[str] | None, is_group: bool) -> list[str]:
    if not names:
        want = [n for n in sections.NAMES if is_group or n != "group_roles"]
    else:
        bad = [n for n in names if n not in sections.NAMES]
        if bad:
            raise ValueError(f"unknown sections: {', '.join(bad)}; known: {', '.join(sections.NAMES)}")
        want = list(dict.fromkeys(names))
    if "capture_gaps" not in want:
        want.append("capture_gaps")
    return want


def _derived_counts(convs: tuple[str, ...]) -> list[list[Any]]:
    """Count and newest time of derived text per source, without reading (or decrypting) the text."""
    return [[str(r["source"]), int(r["count"]), int(r["newest"])] for r in db.derived_stats(convs)]


def _fingerprint(tg: base.Target, lo: int | None, hi: int | None) -> list[Any]:
    chats = [c for c in db.active_chats(after=lo, before=hi) if c["jid"] in tg.jids]
    out: list[Any] = [[c["jid"], c.get("message_count"), c["last_activity"]] for c in chats]
    return [*out, *_derived_counts(tg.jids)]


def _cache_get(key: str, encrypted: bool) -> dict[str, Any] | None:
    if key in _MEMORY:
        _MEMORY.move_to_end(key)
        return copy.deepcopy(_MEMORY[key])
    if encrypted:
        return None
    with state.connect() as conn:
        oldest = int(time.time() - base.settings().cache_days * 86400)
        row = conn.execute(
            "SELECT payload FROM analytics_cache WHERE key = ? AND created_at >= ?", (key, oldest)
        ).fetchone()
    if row is None:
        return None
    value: dict[str, Any] = json.loads(row[0])
    return value


def _cache_put(key: str, chat: str, value: dict[str, Any], encrypted: bool) -> None:
    _MEMORY[key] = copy.deepcopy(value)
    while len(_MEMORY) > 32:
        _MEMORY.popitem(last=False)
    if encrypted:
        return
    with state.connect() as conn, state.transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO analytics_cache (key, chat, created_at, payload) VALUES (?, ?, ?, ?)",
            (key, chat, int(time.time()), json.dumps(value, ensure_ascii=False)),
        )
        state.prune_cache(conn, base.settings().cache_days, _CACHE_ROWS)


def clear_memory_cache() -> None:
    _MEMORY.clear()


def _history(tg: base.Target) -> dict[str, Any]:
    try:
        with state.connect() as conn:
            marks = ", ".join("?" * len(tg.jids))
            rows = conn.execute(
                f"SELECT conv, back_to, state, stopped FROM history_sync WHERE conv IN ({marks})", tg.jids
            ).fetchall()
    except (db.StoreError, sqlite3.Error):
        return {}
    return {
        base.mask(str(r["conv"])): {
            "back_to": db.iso_local(r["back_to"]),
            "state": r["state"],
            "stopped": r["stopped"],
        }
        for r in rows
    }


def _baseline(tg: base.Target, lo: int | None, hi: int | None, s: base.Settings) -> style.Baseline:
    rows = db.messages(after=lo, before=hi, limit=BASELINE_ROWS, ascending=False)
    mine = set(tg.jids)
    by_chat: dict[str, list[base.Msg]] = {}
    keys: dict[str, str] = {}
    others = [
        r for r in rows if r["conv"] not in mine and people.server_of(str(r["conv"])) != people.GROUP_SERVER
    ]
    for m in base.to_msgs(others):
        if m.conv not in keys:
            keys[m.conv] = people.same_person(m.conv)[0]
        by_chat.setdefault(keys[m.conv], []).append(m)
    return style.build_baseline(by_chat, s)


def _compute(
    tg: base.Target, lo: int | None, hi: int | None, want: list[str], s: base.Settings
) -> dict[str, Any]:
    t0 = time.monotonic()
    msgs = base.load(tg.jids, after=lo, before=hi)
    cx = Chat(tg, msgs, s)
    out: dict[str, Any] = {}
    timing: dict[str, int] = {"load": int((time.monotonic() - t0) * 1000)}
    for name in want:
        ts = time.monotonic()
        if name == "phrases":
            out[name] = style.phrases(cx, _baseline(tg, lo, hi, s))
        elif name == "capture_gaps":
            out[name] = style.capture_gaps(cx, _history(tg))
        else:
            out[name] = sections.SECTIONS[name](cx)
        timing[name] = int((time.monotonic() - ts) * 1000)
    meta = {
        "chat": tg.key,
        "name": base.mask(tg.name),
        "is_group": tg.is_group,
        "jids": len(tg.jids),
        "messages": len(msgs),
        "period": [msgs[0].time if msgs else None, msgs[-1].time if msgs else None],
        "after": db.iso_local(lo) if lo else None,
        "before": db.iso_local(hi) if hi else None,
        "sections": want,
        "version": VERSION,
        "timing_ms": timing,
    }
    return {"summary": summary(tg, out), "meta": meta, "sections": out}


def _trim(value: Any, detail: str, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _trim(v, detail, k) for k, v in value.items() if k not in _DROP[detail]}
    if isinstance(value, list):
        cap = DETAIL[detail] if key == "examples" else _LIST_CAP[detail]
        return [_trim(v, detail) for v in value[:cap]]
    return value


def _full(
    chat: str, after: TimeArg, before: TimeArg, names: list[str] | None, refresh: bool
) -> tuple[dict[str, Any], base.Target]:
    s = base.settings()
    tg = base.target(chat)
    lo, hi = to_unix(after), to_unix(before)
    want = _wanted(names, tg.is_group)
    fp = _fingerprint(tg, lo, hi)
    raw = json.dumps(
        [
            VERSION,
            str(db.store().path),
            tg.jids,
            lo,
            hi,
            sorted(want),
            fp,
            s.fingerprint,
            sorted(people.my_jids()),
        ]
    )
    key = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()
    encrypted = db.store().encrypted
    t0 = time.monotonic()
    cached = None if refresh else _cache_get(key, encrypted)
    if cached is not None:
        cached["meta"]["cache"] = "hit"
        cached["meta"]["timing_ms"] = {"total": int((time.monotonic() - t0) * 1000)}
        return cached, tg
    result = _compute(tg, lo, hi, want, s)
    result["meta"]["timing_ms"]["total"] = int((time.monotonic() - t0) * 1000)
    result["meta"]["cache"] = "miss"
    _cache_put(key, tg.key, result, encrypted)
    return result, tg


def analyze(
    chat: str,
    after: TimeArg = None,
    before: TimeArg = None,
    names: list[str] | None = None,
    detail: str = "normal",
    refresh: bool = False,
    report: bool = False,
) -> dict[str, Any]:
    """Analytics for one chat. detail: brief, normal or full. report: also write a Markdown file to exports_dir."""
    if detail not in DETAIL:
        raise ValueError("detail must be brief, normal or full")
    full, tg = _full(chat, after, before, names, refresh)
    out: dict[str, Any] = {
        k: (_trim(v, detail) if k == "sections" else copy.deepcopy(v)) for k, v in full.items()
    }
    out["meta"]["detail"] = detail
    if report:
        out["meta"]["report_path"] = str(write_report(full, tg))
    return out


def write_report(result: dict[str, Any], tg: base.Target) -> Path:
    """Markdown report in exports_dir. It holds plain text from the chat, so it is written 0600."""
    path = db.store().cfg.exports_dir / f"analytics_{fsutil.file_stem(base.mask(tg.name or tg.key))}.md"
    m = result["meta"]
    lines = [f"# Chat analytics: {base.mask(tg.name)}", "", result["summary"], ""]
    lines += [f"Period: {m['period'][0]} to {m['period'][1]}. Messages: {m['messages']}.", ""]
    for name, sec in result["sections"].items():
        lines += [
            f"## {name}",
            "",
            f"Items counted: {sec['n']}" + (" (little data)" if sec["low_data"] else ""),
            "",
        ]
        lines += ["```json", json.dumps(sec["data"], ensure_ascii=False, indent=1, default=str), "```", ""]
        for e in sec["examples"]:
            lines.append(f"- {e['time']} {e['who']}: {e['text']}")
        lines.append("")
    tmp = fsutil.write_private(path.with_suffix(".md.tmp"), "\n".join(lines))
    tmp.replace(path)
    return path


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def summary(tg: base.Target, res: dict[str, Any]) -> str:
    """A few English sentences built from the numbers."""

    def g(sec: str, *keys: str) -> Any:
        return _dig(res.get(sec, {}).get("data"), *keys)

    other = base.mask(tg.name) or "them"
    lines = []
    total = g("volume", "messages", "total")
    if total is not None:
        period = g("volume", "period") or [None, None]
        head = f"Group '{other}'" if tg.is_group else f"Chat with {other}"
        mine = g("volume", "my_share")
        lines.append(
            f"{head}: {total} messages from {str(period[0])[:10]} to {str(period[1])[:10]}, "
            f"{round(100 * mine) if mine is not None else 'n/a'}% from me, {g('volume', 'sessions')} conversations."
        )
    me_lat, them_lat = g("reply_speed", "me", "median_min"), g("reply_speed", "them", "median_min")
    if me_lat is not None:
        s = f"I reply in a median of {fmt_minutes(me_lat)} (p90 {fmt_minutes(g('reply_speed', 'me', 'p90_min'))})"
        within = g("reply_speed", "share_within_5m")
        if within is not None:
            s += f", {round(100 * within)}% within 5 min"
        if them_lat is not None:
            s += f"; {other} replies in {fmt_minutes(them_lat)}"
        deferred = g("deferrals", "share_deferred")
        if deferred is not None:
            s += f"; {round(100 * deferred)}% of incoming turns wait more than 3 h or get no reply"
        lines.append(s + ".")
    w_me, w_them = g("length", ME, "words_median"), g("length", THEM, "words_median")
    if w_me is not None or w_them is not None:
        lines.append(
            f"Median length {num(w_me)} words (them {num(w_them)}); emoji per 100 messages {num(g('emoji', ME, 'per_100_messages'))} "
            f"(them {num(g('emoji', THEM, 'per_100_messages'))}), laughs {num(g('emoji', ME, 'laugh_per_100'))} "
            f"(them {num(g('emoji', THEM, 'laugh_per_100'))})."
        )
    main_me, main_them = g("script", ME, "main"), g("script", THEM, "main")
    if main_me or main_them:
        s = f"Main script: mine {main_me or 'n/a'}, theirs {main_them or 'n/a'}"
        mirror = g("script", "mirror_rate")
        if mirror is not None:
            s += f"; I answer in their script {round(100 * mirror)}% of the time"
        formal = g("formality", ME, "formal_share")
        if formal is not None:
            s += f"; formal markers in {round(100 * formal)}% of my messages"
        lines.append(s + ".")
    st = g("stickers", "total")
    vc = g("voice", "by_side")
    extras = []
    if st:
        extras.append(f"stickers {st} (mine {g('stickers', 'by_side', ME) or 0})")
    if vc:
        extras.append(f"voice notes {sum(vc.values())}, transcribed {g('voice', 'transcribed')}")
    q = g("quotes", "n")
    if q:
        extras.append(f"replies with quote: mine {q.get(ME, 0)}, theirs {q.get(THEM, 0)}")
    if extras:
        lines.append("Also: " + "; ".join(extras) + ".")
    req = g("topics_tasks", "requests")
    if req is not None:
        prom = g("topics_tasks", "promises") or {}
        lines.append(
            f"Requests detected: {req}; my promises: {prom.get('n', 0)} ({prom.get('with_date', 0)} with a date)."
        )
    mine_ph = g("phrases", "mine") or []
    if mine_ph:
        lines.append(
            "Phrases typical for this chat: "
            + ", ".join(f"'{p['phrase']}' x{p['n']}" for p in mine_ph[:4])
            + "."
        )
    if tg.is_group:
        aimed = g("group_roles", "aimed_at_me")
        if aimed is not None:
            ans = g("group_roles", "answered_when_aimed_30m")
            lines.append(
                f"Messages aimed at me: {aimed}, answered within 30 min: "
                f"{round(100 * ans) if ans is not None else 'n/a'}%."
            )
    given = g("reactions", "given") or {}
    if given and sum(given.values()):
        got = g("reactions", "received") or {}
        lines.append(
            f"Reactions: I gave {given.get(ME, 0)} and got {got.get(ME, 0)}; "
            f"{other} gave {given.get(THEM, 0)} and got {got.get(THEM, 0)}."
        )
    gaps = g("volume", "possible_history_gaps") or []
    lines.append(
        "Not visible: polls" + (f"; possible history gaps in {', '.join(gaps[:4])}" if gaps else "") + "."
    )
    return " ".join(lines)


def compare(chats: list[str], after: TimeArg = None, before: TimeArg = None) -> dict[str, Any]:
    """Side by side numbers for 2 to 8 chats, and where a chat differs from the median of the others."""
    if not 2 <= len(chats) <= 8:
        raise ValueError("pass 2 to 8 chats")
    rows: list[dict[str, Any]] = []
    for chat in chats:
        try:
            res, tg = _full(chat, after, before, list(COMPARE_SECTIONS), False)
        except LookupError as exc:
            rows.append({"chat": chat, "error": str(exc)})
            continue

        def g(sec: str, *keys: str, _r: dict[str, Any] = res) -> Any:
            return _dig(_r["sections"].get(sec, {}).get("data"), *keys)

        mine = g("volume", "messages", ME) or 0
        vc = g("voice", "by_side") or {}
        rows.append(
            {
                "chat": chat,
                "jid": tg.key,
                "name": base.mask(tg.name),
                "is_group": tg.is_group,
                "messages": res["meta"]["messages"],
                "my_share": g("volume", "my_share"),
                "reply_median_min": g("reply_speed", "me", "median_min"),
                "their_reply_median_min": g("reply_speed", "them", "median_min"),
                "deferred_share": g("deferrals", "share_deferred"),
                "words_median": g("length", ME, "words_median"),
                "emoji_per_100": g("emoji", ME, "per_100_messages"),
                "laugh_per_100": g("emoji", ME, "laugh_per_100"),
                "stickers_per_100": g("stickers", "per_100_since_first_sticker", ME, "per_100"),
                "voice_share": base.share(vc.get(ME, 0), mine),
                "formal_share": g("formality", ME, "formal_share"),
                "main_script": g("script", ME, "main"),
                "cache": res["meta"]["cache"],
            }
        )
    metrics = (
        "my_share", "reply_median_min", "their_reply_median_min", "deferred_share", "words_median", "emoji_per_100",
        "laugh_per_100", "stickers_per_100", "voice_share", "formal_share",
    )  # fmt: skip
    diffs = []
    for k in metrics:
        vals = [r[k] for r in rows if isinstance(r.get(k), int | float)]
        if len(vals) < 2:
            continue
        mid = stat_median(vals)
        for r in rows:
            v = r.get(k)
            if not isinstance(v, int | float):
                continue
            floor = 1.0 if k.endswith("_min") else 1e-9
            if mid > 0 and (v >= 1.5 * mid or v <= mid / 1.5) and abs(v - mid) > floor:
                diffs.append(
                    {
                        "chat": r["chat"],
                        "metric": k,
                        "value": v,
                        "median": round(mid, 3),
                        "x": round(v / mid, 2),
                    }
                )
            elif mid == 0 and v > 0:
                diffs.append({"chat": r["chat"], "metric": k, "value": v, "median": 0, "x": None})
    for k in ("main_script",):
        values = {r.get(k) for r in rows if r.get(k)}
        if len(values) > 1:
            diffs += [{"chat": r["chat"], "metric": k, "value": r.get(k)} for r in rows if r.get(k)]
    return {
        "rows": rows,
        "differences": diffs,
        "note": "differences: the chat is at least 1.5 times above or below the median of the chats passed",
    }
