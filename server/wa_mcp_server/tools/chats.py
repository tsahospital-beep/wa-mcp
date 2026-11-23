"""Tools: contacts and chats."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import db, people
from wa_mcp_server import outputs
from wa_mcp_server.common import (
    UNTRUSTED,
    Result,
    chat_jids,
    clamp,
    display_name,
    hints,
    ok,
    shape_messages,
    structured,
)
from wa_mcp_server.tools.inputs import Chat, Offset, PageSize


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Search contacts", read_only=True))
    @structured(outputs.Output[outputs.Contacts])
    def search_contacts(
        query: Annotated[
            str,
            Field(
                description="Case-insensitive name, alias or phone fragment, such as Rivera or 55501. A blank query yields no contacts."
            ),
        ],
        limit: Annotated[
            int, Field(description="Maximum matching contacts, up to 200. Zero or negative uses 20.")
        ] = 20,
    ) -> Result:
        """Find a contact by name, alias or phone fragment in the local directory.

        Use list_chats to browse by recent activity. Works offline and in demo. Blank queries and unmatched names give an empty list.
        """
        found = people.search(query, limit=clamp(limit, 20))
        return ok(contacts=found, count=len(found), note=UNTRUSTED)

    @mcp.tool(annotations=hints("List recent chats", read_only=True))
    @structured(outputs.Output[outputs.Chats])
    def list_chats(
        query: Annotated[
            str | None,
            Field(
                description="Filter by stored title or JID substring, such as Team or @g.us. Omit for all chats."
            ),
        ] = None,
        limit: PageSize = 50,
        offset: Offset = 0,
    ) -> Result:
        """Browse conversations in the local archive, most recently active first.

        Use search_contacts for a person or get_chat for one conversation. Works offline and in demo. An empty archive or unmatched filter gives an empty page.
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

    @mcp.tool(annotations=hints("Inspect one chat", read_only=True))
    @structured(outputs.Output[outputs.ChatFound | outputs.Missing])
    def get_chat(
        chat: Chat,
        recent: Annotated[
            int, Field(description="Number of recent messages, up to 200. Zero or negative uses 5.")
        ] = 5,
    ) -> Result:
        """Inspect one conversation and its newest stored messages.

        Use list_messages for a longer history. Works offline and in demo. An unknown name fails, an absent JID gives found=false, and a chat without messages has an empty preview.
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

    @mcp.tool(annotations=hints("Get the last exchange", read_only=True))
    @structured(outputs.Output[outputs.Interaction])
    def get_last_interaction(chat: Chat) -> Result:
        """Find the latest exchange with one contact or group.

        Use get_message_context for neighboring messages. Reads the archive offline and in demo. An unknown name fails, and a direction with no history gives null.
        """
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
