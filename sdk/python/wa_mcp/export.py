"""Markdown transcripts of chats, with transcripts and extracted text inline.

Exports are plain text even when wa.db is encrypted: they are written 0600 into exports_dir, and
attachment paths are shown relative to downloads_dir so the file does not reveal local folder names.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from wa_mcp import db, fsutil, people
from wa_mcp.db import Row, TimeArg, to_unix

_SOURCE_LABEL = {
    "transcript": "Transcript",
    "document": "Document text",
    "archive": "Archive contents",
    "image": "Image text",
    "link": "Linked document",
}


def _stamp(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def render_message(row: Row, derived: list[Row]) -> str:
    who = "me" if row["outgoing"] else people.name_of(str(row["author"]) or str(row["conv"]))
    lines = [f"**{_stamp(int(row['sent_at']))}** {who}:"]
    if row.get("reply_to_id"):
        author = people.name_of(str(row["reply_to_author"])) if row.get("reply_to_author") else "someone"
        preview = (row.get("reply_preview") or "").replace("\n", " ")[:160]
        lines.append(f"  (reply to {author}: {preview})")
    if row["kind"] != "text":
        name = row.get("attachment_name") or ""
        where = (
            f" saved as {_local_name(str(row['local_path']))}" if row.get("local_path") else " not downloaded"
        )
        lines.append(f"  [{row['kind']}{': ' + name if name else ''}]{where}")
    if row["body"]:
        lines.append(_quote(str(row["body"])))
    for d in derived:
        label = _SOURCE_LABEL.get(str(d["source"]), str(d["source"]))
        lang = f" ({d['lang']})" if d.get("lang") else ""
        lines.append(f"  {label}{lang}:")
        lines.append(_quote(str(d["content"])))
    return "\n".join(lines)


def chat_markdown(chat: str, after: TimeArg = None, before: TimeArg = None) -> tuple[str, int]:
    """Markdown for one chat (all JID forms of the person); returns (text, message count)."""
    convs = people.same_person(chat)
    rows = db.messages(conv=convs, after=after, before=before, ascending=True, limit=None)
    derived = db.derived_many([(str(r["conv"]), str(r["msg_id"])) for r in rows])
    title = people.name_of(convs[0])
    parts = [f"## {title}", f"Chat: {', '.join(convs)}", ""]
    for r in rows:
        parts.append(render_message(r, derived.get((str(r["conv"]), str(r["msg_id"])), [])))
        parts.append("")
    return "\n".join(parts), len(rows)


def _period_label(after: TimeArg, before: TimeArg) -> str:
    lo, hi = to_unix(after), to_unix(before)
    a = datetime.fromtimestamp(lo).strftime("%Y-%m-%d") if lo else "start"
    b = datetime.fromtimestamp(hi).strftime("%Y-%m-%d") if hi else "now"
    return f"{a} to {b}"


def _local_name(path: str) -> str:
    """Path relative to downloads_dir; only the file name when the file is elsewhere."""
    p = Path(path)
    try:
        return p.resolve().relative_to(db.store().cfg.downloads_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return p.name


def export_chat(
    chat: str, after: TimeArg = None, before: TimeArg = None, path: str | Path | None = None
) -> dict[str, Any]:
    body, count = chat_markdown(chat, after, before)
    head = f"# WhatsApp export\n\nPeriod: {_period_label(after, before)}\n\n"
    stem = fsutil.file_stem(people.same_person(chat)[0])
    target = Path(path) if path else db.store().cfg.exports_dir / f"{stem}.md"
    fsutil.write_private(target, head + body)
    return {"path": str(target), "messages": count}


def export_period(after: TimeArg, before: TimeArg = None, path: str | Path | None = None) -> dict[str, Any]:
    """One Markdown file with every chat that has messages in the period."""
    seen: set[str] = set()
    sections: list[str] = []
    total = 0
    for chat in db.active_chats(after=after, before=before):
        key = people.same_person(chat["jid"])[0]
        if key in seen:
            continue
        seen.add(key)
        body, count = chat_markdown(key, after, before)
        if count:
            sections.append(body)
            total += count
    label = _period_label(after, before)
    head = f"# WhatsApp export\n\nPeriod: {label}\nChats: {len(sections)}\n\n"
    target = Path(path) if path else db.store().cfg.exports_dir / f"period_{fsutil.file_stem(label)}.md"
    fsutil.write_private(target, head + "\n".join(sections))
    return {"path": str(target), "chats": len(sections), "messages": total}
