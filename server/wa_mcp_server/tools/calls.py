"""Tools: call log."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import db
from wa_mcp_server import outputs
from wa_mcp_server.common import UNTRUSTED, Result, clamp, display_name, hints, ok, structured
from wa_mcp_server.tools.inputs import Offset, PageSize


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("List recorded calls", read_only=True))
    @structured(outputs.Output[outputs.Calls])
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
        """Browse stored voice and video calls, newest first.

        Use get_my_day for missed calls awaiting a response. Works offline and in demo. No matching call records gives an empty page.
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
