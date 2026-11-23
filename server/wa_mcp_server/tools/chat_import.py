"""Tool: add the messages of a WhatsApp "Export chat" file to the local store."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from wa_mcp import db
from wa_mcp.client import BridgeError
from wa_mcp_server.common import (
    LOCAL_WRITE,
    UNTRUSTED,
    Result,
    bridge,
    chat_jids,
    fail,
    importable_file,
    ok,
    safe,
)


def _target_jid(chat: str) -> str:
    """The JID to import into. A person who has a phone chat and a LID chat gets the one that is stored."""
    jids = chat_jids(chat)
    for jid in jids:
        if db.conversation(jid) is not None:
            return jid
    return jids[0]


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=LOCAL_WRITE)
    @safe
    def import_chat_export(
        path: str,
        chat: str,
        me: str | None = None,
        date_order: str | None = None,
        dry_run: bool = False,
        timezone: str | None = None,
        names: dict[str, str] | None = None,
    ) -> Result:
        """Add the messages of a WhatsApp "Export chat" file to a chat, for history older than the phone sync.

        path is a .txt file or a .zip (text plus media) inside downloads_dir, exports_dir or a folder in
        import.allowed_dirs. chat is a JID, a phone number or a chat name. me is the sender name that is
        you, as the export shows it; it is needed unless the file name or the account's push name tells.
        date_order is dmy, mdy or ymd and is detected when the file allows it. timezone is the zone the
        export was made in (default: local time). names maps a sender name to a JID or phone number.

        Run it with dry_run=true first: it reports the counts, the detected date order and how each sender
        name would be stored, and writes nothing. The same file can be imported again without creating
        duplicates, and messages that the phone already synced are skipped. Nothing is sent to anyone. Needs
        the bridge to be running.
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
