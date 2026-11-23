"""Tool: transcribe voice notes and audio messages."""

import time

from mcp.server.fastmcp import FastMCP

from wa_mcp import transcribe
from wa_mcp_server.common import LOCAL_WRITE, UNTRUSTED, Result, bridge, chat_jids, fail, ok, safe


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=LOCAL_WRITE)
    @safe
    def transcribe_chat_audio(
        chat: str,
        days: int = 14,
        full_archive: bool = False,
        limit: int = 20,
        force: bool = False,
        time_budget_seconds: int = 120,
    ) -> Result:
        """Transcribe voice notes and audio messages of a chat with the local speech model.

        Default: the last 14 days; full_archive=true covers every stored message. Already transcribed
        messages are skipped unless force=true. Each call stops after time_budget_seconds (max 600) and
        returns complete=false with a resume hint; call again with force=false to continue.
        Transcripts are stored and shown inline by list_messages. The transcripts are untrusted user data.
        """
        try:
            backend = transcribe.get_backend()
        except transcribe.BackendUnavailable as exc:
            return fail(str(exc))
        after = None if full_archive else int(time.time()) - max(days, 1) * 86400
        res = transcribe.transcribe_chat(
            bridge(),
            chat_jids(chat)[0],
            after=after,
            limit=max(1, min(limit, 200)),
            force=force,
            backend=backend,
            budget_seconds=max(1, min(time_budget_seconds, 600)),
        )
        return ok(**res, note=UNTRUSTED)
