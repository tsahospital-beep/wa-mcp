"""Tools: contacts and chats."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from wa_mcp import db, people
from wa_mcp_server.common import (
    READ_ONLY,
    UNTRUSTED,
    Result,
    chat_jids,
    clamp,
    display_name,
    ok,
    safe,
    shape_messages,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @safe
    def search_contacts(query: str, limit: int = 20) -> Result:
        """Find people by name, alias or part of a phone number.

        Returns each person once with all JIDs (phone form and LID form are merged).
        Use the returned jid as `chat` in other tools.
        """
        found = people.search(query, limit=clamp(limit, 20))
        return ok(contacts=found, count=len(found), note=UNTRUSTED)

    @mcp.tool(annotations=READ_ONLY)
    @safe
    def list_chats(query: str | None = None, limit: int = 50, offset: int = 0) -> Result:
        """List chats (people and groups) by most recent activity, optionally filtered by title or JID.

        One person may have two JIDs (phone and LID); they are shown as one entry with both JIDs.
        """
        offset = max(offset, 0)
        rows = db.conversations(query=query, limit=clamp(limit), offset=offset)
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            jids = people.same_person(row["jid"])
            entry = merged.setdefault(
                jids[0],
                {
                    "jid": jids[0],
                    "jids": jids,
                    "name": display_name(jids[0]),
                    "name_source": people.name_info(jids[0])[1],
                    "is_group": row["is_group"],
                    "last_activity": row["time"],
                },
            )
            if row["time"] and (entry["last_activity"] or "") < row["time"]:
                entry["last_activity"] = row["time"]
        return ok(chats=list(merged.values()), offset=offset, next_offset=offset + len(rows), note=UNTRUSTED)

    @mcp.tool(annotations=READ_ONLY)
    @safe
    def get_chat(chat: str, recent: int = 5) -> Result:
        """Returns found=false for a chat absent from the store. Otherwise returns its name, all JIDs, group flag and the most recent messages.

        `chat` may be a JID, a phone number with country code, or a chat or contact name.
        """
        jids = chat_jids(chat)
        info = next((c for c in (db.conversation(j) for j in jids) if c), None)
        if info is None:
            return ok(found=False)
        rows = db.messages(conv=jids, limit=clamp(recent, 5))
        return ok(
            found=True,
            chat={
                "jid": jids[0],
                "jids": jids,
                "name": display_name(jids[0]),
                "name_source": people.name_info(jids[0])[1],
                "is_group": bool(info and info["is_group"]),
                "phone": people.phone_of(jids[0]),
            },
            recent_messages=shape_messages(rows),
            note=UNTRUSTED,
        )

    @mcp.tool(annotations=READ_ONLY)
    @safe
    def get_last_interaction(chat: str) -> Result:
        """When we last talked with a person or group: latest message overall, latest from them, latest from me."""
        jids = chat_jids(chat)
        rows = [
            row for direction in (False, True) for row in db.messages(conv=jids, outgoing=direction, limit=1)
        ]
        rows.sort(key=lambda row: (row["sent_at"], row["msg_id"]), reverse=True)
        shaped = shape_messages(rows)
        by_direction = {r["outgoing"]: s for r, s in zip(rows, shaped, strict=True)}
        return ok(
            chat=jids[0],
            name=display_name(jids[0]),
            last=shaped[0] if shaped else None,
            last_from_them=by_direction.get(False),
            last_from_me=by_direction.get(True),
            note=UNTRUSTED,
        )
