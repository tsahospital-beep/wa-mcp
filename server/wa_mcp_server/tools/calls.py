"""Tools: call log."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import db
from wa_mcp_server.common import UNTRUSTED, Result, clamp, display_name, hints, ok, safe
from wa_mcp_server.tools.inputs import Offset, PageSize


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("List recorded calls", read_only=True))
    @safe
    def list_calls(
        since: Annotated[
            str | None,
            Field(
                description="Earliest call start, inclusive. Use Unix seconds as text, YYYY-MM-DD or an ISO timestamp such as 2026-11-23T09:00:00Z. Omit for all calls."
            ),
        ] = None,
        limit: PageSize = 50,
        offset: Offset = 0,
    ) -> Result:
        """Read the local voice and video call log, newest first. Returns calls with id, peer, time,
        direction, video, outcome and duration_seconds, plus next_offset. Phone-originated calls depend on
        synced records. Use my_day for missed calls still awaiting a response.
        """
        start = max(offset, 0)
        rows = db.calls(since=since, limit=clamp(limit), offset=start)
        items = [
            {
                "id": r["call_id"],
                "with": display_name(r["peer"]) if r["peer"] else None,
                "peer": r["peer"],
                "group": r.get("group_jid"),
                "time": r["time"],
                "direction": "outgoing" if r["outgoing"] else "incoming",
                "video": r["video"],
                "outcome": r["outcome"],
                "duration_seconds": r["duration_seconds"],
            }
            for r in rows
        ]
        return ok(calls=items, next_offset=start + len(items), note=UNTRUSTED)
