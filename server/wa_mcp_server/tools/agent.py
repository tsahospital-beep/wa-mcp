"""Agent controls and review of stored reply drafts."""

import contextlib
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from wa_mcp.agent import worker
from wa_mcp.agent.providers import diagnostic
from wa_mcp.insight import state
from wa_mcp_server import outputs
from wa_mcp_server.common import (
    Result,
    blocking,
    bridge,
    hints,
    ok,
    structured,
)
from wa_mcp_server.tools import send
from wa_mcp_server.tools.inputs import Confirm


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
    @mcp.tool(annotations=hints("Check reply agent status", read_only=True))
    @structured(outputs.Output[outputs.AgentStatus])
    def get_agent_status() -> Result:
        """Inspect the reply agent's configuration and local review queue.

        Use test_agent_provider to exercise the provider. Works offline and in demo. An empty queue shows zero pending drafts.
        """
        a = worker.current().agent
        last_poll = None
        drafts = 0
        with state.read_existing("agent_drafts") as conn:
            if conn is not None:
                last_poll = state.get_meta(conn, "agent_last_poll")
                drafts = conn.execute("SELECT count(*) FROM agent_drafts WHERE status='drafted'").fetchone()[
                    0
                ]
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

    @mcp.tool(annotations=hints("List pending reply drafts", read_only=True))
    @structured(outputs.Output[outputs.Drafts])
    def list_agent_drafts(
        limit: Annotated[
            int, Field(description="Maximum pending drafts to return, clamped to 1 through 100.")
        ] = 50,
    ) -> Result:
        """List pending agent replies for human review, newest first.

        Use approve_agent_draft to act on one. Reads locally offline and in demo. With no pending drafts the list is empty.
        """
        return ok(drafts=worker.pending(limit=limit, with_audio=False))

    @mcp.tool(annotations=hints("Review and send a reply draft", destructive=True, open_world=True))
    @structured(outputs.Output[outputs.Decision])
    async def approve_agent_draft(
        draft_id: Annotated[
            str,
            Field(
                description="Use id from list_agent_drafts, a 32-digit hex string such as 09abc12309abc12309abc12309abc123."
            ),
        ],
        ctx: Context,  # type: ignore[type-arg]
        confirm: Confirm = False,
    ) -> Result:
        """Review a pending agent reply and send it only after human approval.

        Use list_agent_drafts to choose the draft. Declining discards it. Quiet hours can queue it. Unknown ids, disabled sending and demo block delivery. Delivery needs the bridge.
        """
        with review(draft_id) as (draft, checked):
            refused = await send.approve(ctx, "Send this agent reply?", checked, confirm, required=True)
            if refused:
                if refused.get("error") == send.DECLINED:
                    await blocking(worker.decline, bridge(), draft)
                return refused
            result = await blocking(worker.deliver, bridge(), draft)
            return {"ok": result["status"] in ("sent", "queued"), **result}

    @mcp.tool(annotations=hints("Pause the reply agent", destructive=True, idempotent=True, open_world=True))
    @structured(outputs.Output[outputs.AgentSwitch])
    def pause_agent() -> Result:
        """Pause future automatic replies and cancel this account's pending agent sends.

        Use get_agent_status to inspect the switch. Pausing is local, but cancelling queued deliveries needs the bridge. Demo refuses.
        """
        worker.set_paused(True)
        return ok(paused=True)

    @mcp.tool(annotations=hints("Resume the reply agent", destructive=True, idempotent=True, open_world=True))
    @structured(outputs.Output[outputs.AgentSwitch])
    def resume_agent() -> Result:
        """Allow the reply agent to handle future events for this account.

        Use get_agent_status to inspect policy first. The local switch works offline. Replies still require enabled sending and a bridge and may incur provider charges. Demo refuses.
        """
        worker.set_paused(False)
        return ok(paused=False)

    @mcp.tool(annotations=hints("Test the reply provider", open_world=True))
    @structured(outputs.Output[outputs.ProviderTest])
    async def test_agent_provider(
        voice: Annotated[
            bool,
            Field(
                description="Enable the configured voice test and return its local preview file when available."
            ),
        ] = False,
    ) -> Result:
        """Exercise the reply provider with synthetic input without sending a WhatsApp message.

        Use get_agent_status for a local check. Works without the bridge. Providers may charge and the optional voice test writes a preview. Demo refuses.
        """
        try:
            return ok(**await worker.test_connection(worker.current(), voice))
        except Exception as exc:
            return {"ok": False, "error": diagnostic(exc)}
