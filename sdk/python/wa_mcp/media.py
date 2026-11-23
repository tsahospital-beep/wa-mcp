"""Download attachments through the bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wa_mcp import db, people
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.db import Row, TimeArg

# Kinds with a downloadable file (a subset of db.KINDS).
MEDIA_KINDS = ("image", "video", "ptv", "audio", "voice", "document", "sticker")


def local_file(row: Row) -> Path | None:
    """The saved attachment of a message row, if it still exists on disk."""
    path = row.get("local_path")
    if path and Path(path).is_file():
        return Path(path)
    return None


def ensure_local(bridge: Bridge, row: Row) -> Path:
    """Path of the attachment, downloading it through the bridge when needed."""
    found = local_file(row)
    if found:
        return found
    if row.get("kind") not in MEDIA_KINDS:
        raise ValueError(f"message {row.get('msg_id')} has no attachment")
    data = bridge.download_media(str(row["conv"]), str(row["msg_id"]))
    return Path(str(data["path"]))


def download_media(bridge: Bridge, chat: str, msg_id: str) -> dict[str, Any]:
    """Save one attachment; chat may be any JID form of the conversation."""
    for conv in people.same_person(chat):
        row = db.message(conv, msg_id)
        if row is not None:
            path = ensure_local(bridge, row)
            return {"chat": conv, "msg_id": msg_id, "kind": row["kind"], "path": str(path)}
    raise LookupError(f"message {msg_id} not found in {chat}")


def download_chat_media(
    bridge: Bridge,
    chat: str,
    after: TimeArg = None,
    before: TimeArg = None,
    kinds: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Save every attachment of a chat in a period; already saved files are reused."""
    wanted = [k for k in (kinds or list(MEDIA_KINDS)) if k in MEDIA_KINDS]
    if not wanted:
        raise ValueError(f"kinds must be some of {', '.join(MEDIA_KINDS)}")
    rows = db.messages(conv=people.same_person(chat), after=after, before=before, kind=wanted, limit=limit)
    saved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    reused = 0
    for row in rows:
        if local_file(row):
            reused += 1
            saved.append({"msg_id": row["msg_id"], "kind": row["kind"], "path": row["local_path"]})
            continue
        try:
            path = ensure_local(bridge, row)
        except BridgeError as exc:
            failed.append({"msg_id": row["msg_id"], "kind": row["kind"], "error": exc.message})
            if exc.status in (0, 401, 503):
                break
            continue
        saved.append({"msg_id": row["msg_id"], "kind": row["kind"], "path": str(path)})
    return {"saved": saved, "failed": failed, "already_local": reused, "matched": len(rows)}
