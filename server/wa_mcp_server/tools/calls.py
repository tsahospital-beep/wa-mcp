"""Tools: call log."""

from mcp.server.fastmcp import FastMCP

from wa_mcp import db
from wa_mcp_server.common import READ_ONLY, UNTRUSTED, Result, clamp, display_name, ok, safe


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @safe
    def list_calls(since: str | None = None, limit: int = 50, offset: int = 0) -> Result:
        """List voice and video calls, newest first, with outcome (answered, missed, rejected, unknown).

        since: unix seconds, YYYY-MM-DD or ISO 8601.
        """
        start = max(offset, 0)
        rows = db.calls(since=since, limit=start + clamp(limit))[start:]
        items = [
            {
                "id": r["call_id"],
                "with": display_name(r["peer"]),
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
