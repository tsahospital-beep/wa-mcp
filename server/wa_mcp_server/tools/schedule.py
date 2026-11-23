"""Tools: scheduled messages."""

from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from wa_mcp import db
from wa_mcp_server import outputs
from wa_mcp_server.common import (
    UNTRUSTED,
    Result,
    blocking,
    bridge,
    clamp,
    display_name,
    fail,
    hints,
    ok,
    sendable_file,
    structured,
)
from wa_mcp_server.tools.inputs import Confirm, Mentions, Offset, PageSize, Recipient, ReplyTo
from wa_mcp_server.tools.send import approve, plan

RECURRENCES = ("once", "daily", "weekly", "monthly")
STATUSES = ("pending", "sending", "sent", "failed", "cancelled")


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Schedule a WhatsApp message", open_world=True))
    @structured(outputs.Output[outputs.Scheduled])
    async def schedule_message(
        to: Recipient,
        at: Annotated[
            str,
            Field(
                description="Delivery time as an ISO 8601 timestamp with offset (2026-12-01T09:00:00+04:00) or Unix seconds as text. Use a future time."
            ),
        ],
        ctx: Context,  # type: ignore[type-arg]
        text: Annotated[
            str | None,
            Field(
                description="Text body (up to 12000 characters), or caption (up to 4096 with media). Required when media_path is omitted."
            ),
        ] = None,
        media_path: Annotated[
            str | None,
            Field(
                description="Server-local attachment inside downloads_dir, exports_dir or sending.allowed_media_dirs. Example /exports/invoice.pdf. Keep it available until delivery. Required when text is omitted."
            ),
        ] = None,
        recurrence: Annotated[
            str, Field(description="Delivery frequency: once, daily, weekly or monthly.")
        ] = "once",
        reply_to: ReplyTo = None,
        mentions: Mentions = None,
        confirm: Confirm = False,
    ) -> Result:
        """Schedule a one-time or recurring delivery through the bridge.

        Use send_message for immediate delivery. Each call creates another schedule. Demo, disabled sending, an unresolved recipient or an offline bridge blocks scheduling.
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

    @mcp.tool(annotations=hints("List scheduled messages", read_only=True))
    @structured(outputs.Output[outputs.Queue])
    def list_scheduled_messages(
        status: Annotated[
            str | None,
            Field(
                description="Filter by pending, sending, sent, failed or cancelled. Omit to include all states."
            ),
        ] = None,
        limit: PageSize = 50,
        offset: Offset = 0,
    ) -> Result:
        """Read scheduled deliveries in next-run order without triggering delivery.

        Use cancel_scheduled_message to stop one. Works offline and in demo, including with sending disabled. An empty queue or unmatched status gives an empty page.
        """
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
                **(
                    {
                        "action": r["kind"],
                        "source_chat": r["source_chat"],
                        "message_id": r["source_message_id"],
                    }
                    if r.get("source_chat")
                    else {}
                ),
                "media_path": r.get("media_path"),
                "next_run": r["time"],
                "recurrence": r["recurrence"],
                "status": r["status"],
                "last_error": r.get("last_error"),
            }
            for r in page
        ]
        return ok(scheduled=items, total=total, next_offset=start + len(page), note=UNTRUSTED)

    @mcp.tool(annotations=hints("Cancel a pending send", destructive=True, idempotent=True))
    @structured(outputs.Output[outputs.Cancelled])
    def cancel_scheduled_message(
        scheduled_id: Annotated[
            int,
            Field(
                description="Positive integer id from list_scheduled_messages or schedule_message, such as 42. A chat message id identifies a different object."
            ),
        ],
    ) -> Result:
        """Cancel a pending delivery, including future runs of a recurring schedule.

        Use list_scheduled_messages to find its id. Already dispatched messages cannot be recalled. Requires the bridge and is unavailable in demo.
        """
        res = bridge().cancel_scheduled(scheduled_id)
        return ok(cancelled=bool(res.get("cancelled")), id=scheduled_id)
