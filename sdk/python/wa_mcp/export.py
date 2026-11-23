"""Markdown transcripts of chats, with transcripts and extracted text inline.

Exports are plain text even when wa.db is encrypted: they are written 0600 into exports_dir, and
attachment paths are shown relative to downloads_dir so the file does not reveal local folder names.
"""

from __future__ import annotations

import io
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
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


def render_message(row: Row, derived: list[Row], name_of: Callable[[str], str] = people.name_of) -> str:
    who = "me" if row["outgoing"] else name_of(str(row["author"]) or str(row["conv"]))
    lines = [f"**{_stamp(int(row['sent_at']))}** {who}:"]
    if row.get("reply_to_id"):
        author = name_of(str(row["reply_to_author"])) if row.get("reply_to_author") else "someone"
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
    output = io.StringIO()
    count = _write_chat(output.write, chat, after, before, include_empty=True)
    return output.getvalue(), count


def _write_chat(
    write: Callable[[str], int],
    chat: str,
    after: TimeArg,
    before: TimeArg,
    *,
    include_empty: bool,
    separator: str = "",
) -> int:
    convs = people.same_person(chat)
    name = lru_cache(maxsize=256)(people.name_of)
    header = f"{separator}## {name(convs[0])}\nChat: {', '.join(convs)}\n"
    count = 0
    for row, derived in db.store().transcript_rows(convs, after, before):
        if count == 0:
            write(header)
        write("\n" + render_message(row, derived, name) + "\n")
        count += 1
    if include_empty and count == 0:
        write(header)
    return count


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
    head = f"# WhatsApp export\n\nPeriod: {_period_label(after, before)}\n\n"
    stem = fsutil.file_stem(people.same_person(chat)[0])
    target = Path(path) if path else db.store().cfg.exports_dir / f"{stem}.md"
    with fsutil.private_writer(target) as out:
        out.write(head)
        count = _write_chat(out.write, chat, after, before, include_empty=True)
    return {"path": str(target), "messages": count}


def export_period(after: TimeArg, before: TimeArg = None, path: str | Path | None = None) -> dict[str, Any]:
    """One Markdown file with every chat that has messages in the period."""
    label = _period_label(after, before)
    target = Path(path) if path else db.store().cfg.exports_dir / f"period_{fsutil.file_stem(label)}.md"
    seen: set[str] = set()
    total = chats = 0
    with (
        fsutil.private_writer(target) as out,
        tempfile.TemporaryFile(mode="w+", dir=target.parent, encoding="utf-8", newline="\n") as sections,
    ):
        for chat in db.active_chats(after=after, before=before):
            key = people.same_person(chat["jid"])[0]
            if key in seen:
                continue
            seen.add(key)
            count = _write_chat(
                sections.write, key, after, before, include_empty=False, separator="\n" if chats else ""
            )
            if count:
                chats += 1
                total += count
        out.write(f"# WhatsApp export\n\nPeriod: {label}\nChats: {chats}\n\n")
        sections.seek(0)
        shutil.copyfileobj(sections, out)
    return {"path": str(target), "chats": chats, "messages": total}
