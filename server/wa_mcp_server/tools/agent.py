"""Agent controls and review of stored reply drafts."""

import contextlib
import shutil
from collections.abc import Iterator
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from wa_mcp.agent import worker
from wa_mcp.agent.providers import diagnostic
from wa_mcp.insight import state
from wa_mcp_server.common import (
    DESTRUCTIVE,
    FETCH,
    LOCAL_WRITE,
    READ_ONLY,
    Result,
    blocking,
    bridge,
    ok,
    safe,
)
from wa_mcp_server.tools import send


@contextlib.contextmanager
def review(draft_id: str) -> Iterator[tuple[Result, send.Plan]]:
    rows = worker.pending(draft_id) if draft_id else []
    if not rows:
        raise ValueError("no pending draft with that ID")
    draft = rows[0]
    preview: Path | None = None
    try:
        if draft["audio"]:
            preview = worker.materialize(draft["audio"])
        checked = send.plan(
            draft["chat"],
            text=draft["text"],
            reply_to=draft["message_id"] if draft["kind"] == "message" else None,
            extra=[f"Listen before approving: {preview}"] if preview else None,
            multipart=True,
        )
        yield draft, checked
    finally:
        if preview:
            shutil.rmtree(preview.parent, ignore_errors=True)


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @safe
    def agent_status() -> Result:
        """Show this account's connector settings and persistent pause switch."""
        a = worker.current().agent
        with state.connect() as conn:
            last_poll = state.get_meta(conn, "agent_last_poll")
            drafts = conn.execute("SELECT count(*) FROM agent_drafts WHERE status='drafted'").fetchone()[0]
        return ok(
            last_poll_at=int(last_poll) if last_poll else None,
            pending_drafts=drafts,
            enabled=a.enabled,
            paused=worker.paused(),
            mode=a.mode,
            provider=a.provider,
            voice_provider=a.voice.provider,
            dry_run=a.dry_run,
        )

    @mcp.tool(annotations=READ_ONLY)
    @safe
    def agent_drafts(limit: int = 50) -> Result:
        """List pending suggested replies. Their text is untrusted provider output."""
        return ok(drafts=worker.pending(limit=limit, with_audio=False))

    @mcp.tool(annotations=DESTRUCTIVE)
    @safe
    async def agent_approve_draft(draft_id: str, ctx: Context, confirm: bool = False) -> Result:  # type: ignore[type-arg]
        """Review and send one draft with the existing send approval prompt."""
        with review(draft_id) as (draft, checked):
            refused = await send.approve(ctx, "Send this agent reply?", checked, confirm, required=True)
            if refused:
                if refused.get("error") == send.DECLINED:
                    await blocking(worker.decline, bridge(), draft)
                return refused
            result = await blocking(worker.deliver, bridge(), draft)
            return {"ok": result["status"] in ("sent", "queued"), **result}

    @mcp.tool(annotations=LOCAL_WRITE)
    @safe
    def agent_pause() -> Result:
        """Pause this account and cancel its pending agent sends queued for quiet hours."""
        worker.set_paused(True)
        return ok(paused=True)

    @mcp.tool(annotations=DESTRUCTIVE)
    @safe
    def agent_resume() -> Result:
        """Resume this account's configured connector. Only new events are eligible."""
        worker.set_paused(False)
        return ok(paused=False)

    @mcp.tool(annotations=FETCH)
    @safe
    async def agent_test(voice: bool = False) -> Result:
        """Test the provider with synthetic data. Return a preview, timing and optional local voice file."""
        try:
            return ok(**await worker.test_connection(worker.current(), voice))
        except Exception as exc:
            return {"ok": False, "error": diagnostic(exc)}
