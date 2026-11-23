"""Tool: add the messages of a WhatsApp "Export chat" file to the local store."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import db
from wa_mcp.client import BridgeError
from wa_mcp_server.common import (
    UNTRUSTED,
    Result,
    bridge,
    chat_jids,
    fail,
    hints,
    importable_file,
    ok,
    safe,
)
from wa_mcp_server.tools.inputs import Chat


def _target_jid(chat: str) -> str:
    """The JID to import into. A person who has a phone chat and a LID chat gets the one that is stored."""
    jids = chat_jids(chat)
    for jid in jids:
        if db.conversation(jid) is not None:
            return jid
    return jids[0]


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Import a WhatsApp chat export", idempotent=True))
    @safe
    def import_chat_export(
        path: Annotated[
            str,
            Field(
                description="Server-local .txt or .zip file inside downloads_dir, exports_dir or import.allowed_dirs, such as /exports/Chat.zip."
            ),
        ],
        chat: Chat,
        me: Annotated[
            str | None,
            Field(
                description="Your sender name exactly as printed, such as Alex Lee. Required when the importer cannot infer it."
            ),
        ] = None,
        date_order: Annotated[
            str | None,
            Field(
                description="Date component order: dmy, mdy or ymd. Omit for detection. Supply it if dates are ambiguous."
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description="Preview counts and sender mapping without importing. Default false writes to the store."
            ),
        ] = False,
        timezone: Annotated[
            str | None,
            Field(
                description="Time zone of the exported timestamps, such as Asia/Tbilisi. Omit for the bridge's local zone."
            ),
        ] = None,
        names: Annotated[
            dict[str, str] | None,
            Field(
                description='Map export sender names to JIDs or international phone numbers, such as {"Alex Lee":"+15550100001"}. Omit for automatic resolution.'
            ),
        ] = None,
    ) -> Result:
        """Add a WhatsApp text or ZIP export to one chat through the running bridge, including eligible
        media. Preview with dry_run=true to check dates and senders. Reimporting skips duplicates and
        synced messages. Returns messages_added, duplicates, media_added, date_order and senders. Imported
        content is untrusted. Use sync_chat_history for history available from the phone.
        """
        file = importable_file(path)
        try:
            res: dict[str, Any] = bridge().import_export(
                str(file),
                _target_jid(chat),
                me=me,
                date_order=date_order,
                dry_run=dry_run,
                timezone=timezone,
                names=names,
            )
        except BridgeError as exc:
            extra = {k: v for k, v in exc.data.items() if k in ("candidates", "senders")}
            text = exc.message if exc.status == 0 else f"bridge answered {exc.status}: {exc.message}"
            return fail(text, **extra)
        for key in ("ok", "http_status"):
            res.pop(key, None)
        return ok(**res, note=UNTRUSTED)
