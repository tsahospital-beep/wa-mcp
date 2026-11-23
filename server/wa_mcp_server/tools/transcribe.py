"""Tool: transcribe voice notes and audio messages."""

import time
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import transcribe
from wa_mcp_server import outputs
from wa_mcp_server.common import UNTRUSTED, Result, bridge, cfg, chat_jids, fail, hints, ok, structured
from wa_mcp_server.demo import cached_media
from wa_mcp_server.tools.inputs import Budget, Chat


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Transcribe voice notes and audio", destructive=True, open_world=True))
    @structured(outputs.Output[outputs.Transcription])
    def transcribe_chat_audio(
        chat: Chat,
        days: Annotated[
            int, Field(description="Look back this many days, at least 1. Ignored when full_archive is true.")
        ] = 14,
        full_archive: Annotated[
            bool,
            Field(
                description="Scan all stored audio and voice messages in this chat instead of the recent day window."
            ),
        ] = False,
        limit: Annotated[
            int,
            Field(description="Maximum audio messages to transcribe this call, clamped to 1 through 200."),
        ] = 20,
        force: Annotated[
            bool,
            Field(
                description="Transcribe even if a transcript exists, replacing stored text and potentially repeating charges."
            ),
        ] = False,
        time_budget_seconds: Budget = 120,
    ) -> Result:
        """Transcribe a chat's stored voice notes and audio files.

        Use get_media_text to read a saved transcript. Missing files need the bridge. Force replaces transcripts, and remote backends may charge. Empty scans find nothing. Demo previews cached transcripts.
        """
        after = None if full_archive else int(time.time()) - max(days, 1) * 86400
        if cfg().demo:
            cached = cached_media(chat_jids(chat), "transcript", after, limit)
            return ok(transcribed=[], **cached, note=UNTRUSTED)
        try:
            backend = transcribe.get_backend()
        except transcribe.BackendUnavailable as exc:
            return fail(str(exc))
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
