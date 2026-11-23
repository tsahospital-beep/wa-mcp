"""Watched words and phrases (config mentions.watch) found in any chat, with a little context.

mentions.watch entries are phrases ("quarterly report") or {"label": "Report", "words": ["report", "q3 numbers"]}.
Voice transcripts and extracted file text are searched too. The search runs in SQL over the time window,
newest first, and stops as soon as the requested page is filled.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

from wa_mcp import db
from wa_mcp.insight import base
from wa_mcp.insight.base import Msg
from wa_mcp.insight.wordlists import Words, fold_map

MAX_DAYS = 3650
_CHUNK = 200


def _around(text: str, hit: str, n: int = 160) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    folded, index = fold_map(flat)
    found = folded.find(hit.rstrip("*"))
    if found < 0 or len(flat) <= n:
        return base.clip(flat, n)
    pos = index[found]
    start = max(0, pos - (n - len(hit)) // 2)
    piece = flat[start : start + n]
    return ("..." if start else "") + piece.strip() + ("..." if start + n < len(flat) else "")


def _searchable(m: Msg) -> str:
    return "\n".join(t for t in (m.text, m.file_name, m.file_text) if t)


def _stream(item: str, conv: list[str] | None, after: int, before: int) -> Iterator[Msg]:
    """Messages whose text contains item (substring in SQL), newest first, a chunk at a time."""
    query = item[:-1] if item.endswith("*") else item
    offset = 0
    while True:
        rows = db.messages(conv=conv, after=after, before=before, query=query, limit=_CHUNK, offset=offset)
        yield from base.to_msgs(rows)
        if len(rows) < _CHUNK:
            return
        offset += _CHUNK


def find(
    days: float = 7,
    words: list[str] | None = None,
    chat: list[str] | None = None,
    include_me: bool = False,
    limit: int = 50,
    offset: int = 0,
    now: float | None = None,
) -> dict[str, Any]:
    """Messages of the last `days` days that contain a watched word (or the given words instead), newest first."""
    if not 0 < days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    s = base.settings()
    watch: list[tuple[str, Words]] = [(w, Words([w])) for w in words or [] if w.strip()] or list(s.watch)
    if not watch:
        return {"watch": [], "count": 0, "has_more": False, "mentions": [], "note": "nothing to watch"}
    now_ts = int(time.time() if now is None else now)
    after, before = now_ts - int(days * 86400), now_ts + 1
    want = max(offset, 0) + max(limit, 0) + 1
    hits: dict[tuple[str, str], dict[str, Any]] = {}
    for label, w in watch:
        for item in w.items:
            got = 0
            for m in _stream(item, chat, after, before):
                if m.me and not include_me:
                    continue
                matched = w.find(_searchable(m))
                if not matched:
                    continue
                entry = hits.setdefault((m.conv, m.id), {"msg": m, "watch": [], "matched": set()})
                if label not in entry["watch"]:
                    entry["watch"].append(label)
                entry["matched"].update(matched)
                got += 1
                if got >= want:
                    break
    ordered = sorted(hits.values(), key=lambda e: (-e["msg"].ts, e["msg"].id))
    page = ordered[max(offset, 0) : max(offset, 0) + max(limit, 0)]
    names = base.Names()
    out = []
    per_watch: dict[str, int] = {}
    for e in page:
        hit: Msg = e["msg"]
        ctx = db.context(hit.conv, hit.id, before=1, after=1) or {"before": [], "after": []}
        for label in e["watch"]:
            per_watch[label] = per_watch.get(label, 0) + 1
        out.append(
            {
                "time": hit.time,
                "chat": hit.conv,
                "chat_name": names(hit.conv),
                "from": names(hit.who),
                "message_id": hit.id,
                "watch": e["watch"],
                "matched": sorted(e["matched"]),
                "text": _around(_searchable(hit), sorted(e["matched"])[0]),
                "context": {
                    "before": base.clip(str(ctx["before"][0]["body"]), 120) if ctx["before"] else None,
                    "after": base.clip(str(ctx["after"][0]["body"]), 120) if ctx["after"] else None,
                },
            }
        )
    return {
        "watch": [label for label, _ in watch],
        "count": len(out),
        "has_more": len(ordered) > max(offset, 0) + len(out),
        "by_watch": per_watch,
        "mentions": out,
        "next_offset": max(offset, 0) + len(out),
    }
