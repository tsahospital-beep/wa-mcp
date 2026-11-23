"""Tools: scheduled messages."""

from mcp.server.fastmcp import Context, FastMCP

from wa_mcp import db
from wa_mcp_server.common import (
    DESTRUCTIVE,
    READ_ONLY,
    UNTRUSTED,
    Result,
    blocking,
    bridge,
    clamp,
    display_name,
    fail,
    ok,
    safe,
    sendable_file,
)
from wa_mcp_server.tools.send import approve, plan

RECURRENCES = ("once", "daily", "weekly", "monthly")
STATUSES = ("pending", "sending", "sent", "failed", "cancelled")


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=DESTRUCTIVE)
    @safe
    async def schedule_message(
        to: str,
        at: str,
        ctx: Context,  # type: ignore[type-arg]
        text: str | None = None,
        media_path: str | None = None,
        recurrence: str = "once",
        reply_to: str | None = None,
        mentions: list[str] | None = None,
        confirm: bool = False,
    ) -> Result:
        """Schedule a message for later. at: ISO 8601 time or unix seconds. recurrence: once, daily, weekly, monthly.

        The bridge sends it at that time even if this assistant is closed. Recipient rules, file folders and
        approval as in send_message and send_file.
        """
        if recurrence not in RECURRENCES:
            return fail(f"recurrence must be one of {', '.join(RECURRENCES)}")
        if not (text or media_path):
            return fail("give text or media_path")
        media = str(await blocking(sendable_file, media_path)) if media_path else None
        extra = [f"When: {at} ({recurrence})"] + ([f"File: {media}"] if media else [])
        checked = await blocking(
            plan, to, text=text, reply_to=reply_to, mentions=mentions, extra=extra, multipart=media is None
        )
        refused = await approve(ctx, "Schedule this message?", checked, confirm)
        if refused:
            return refused
        res = await blocking(
            bridge().schedule,
            checked.target,
            at,
            text=text,
            media_path=media,
            reply_to=reply_to,
            mentions=mentions,
            recurrence=recurrence,
        )
        name = await blocking(display_name, checked.target)
        return ok(
            id=res.get("id"), next_run=db.iso_local(res.get("next_run")), to=checked.target, to_name=name
        )

    @mcp.tool(annotations=READ_ONLY)
    @safe
    def list_scheduled(status: str | None = None, limit: int = 50, offset: int = 0) -> Result:
        """List scheduled messages, soonest first. status: pending, sending, sent, failed or cancelled."""
        if status and status not in STATUSES:
            return fail(f"status must be one of {', '.join(STATUSES)}")
        start = max(offset, 0)
        page, total = db.store().scheduled_page(status, clamp(limit), start)
        items = [
            {
                "id": r["id"],
                "to": r["target"],
                "to_name": display_name(r["target"]),
                "text": r["body"],
                "media_path": r.get("media_path"),
                "next_run": r["time"],
                "recurrence": r["recurrence"],
                "status": r["status"],
                "last_error": r.get("last_error"),
            }
            for r in page
        ]
        return ok(scheduled=items, total=total, next_offset=start + len(page), note=UNTRUSTED)

    @mcp.tool(annotations=DESTRUCTIVE)
    @safe
    def cancel_scheduled(scheduled_id: int) -> Result:
        """Cancel a pending scheduled message by its id (from list_scheduled)."""
        res = bridge().cancel_scheduled(scheduled_id)
        return ok(cancelled=bool(res.get("cancelled")), id=scheduled_id)
