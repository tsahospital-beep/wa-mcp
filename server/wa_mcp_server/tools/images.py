"""Tool: read the text of photos and screenshots, and describe them with an optional AI provider."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import ai, vision
from wa_mcp_server import outputs
from wa_mcp_server.common import UNTRUSTED, Result, bridge, cfg, chat_jids, fail, hints, ok, structured
from wa_mcp_server.demo import cached_media
from wa_mcp_server.tools.inputs import Budget, Chat

NO_OCR = (
    "reading text from images is off: set vision.ocr to tesseract or rapidocr in config.json "
    "(describe=true works with ai.provider alone)"
)


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Read text and descriptions from images", destructive=True, open_world=True))
    @structured(outputs.Output[outputs.Images])
    def read_images(
        chat: Chat,
        since: Annotated[
            str | None,
            Field(
                description="Only images sent strictly after this time. Use Unix seconds as text, YYYY-MM-DD or ISO 8601, such as 2026-11-01. Omit for all stored images."
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum images to process this call, clamped to 1 through 200.")
        ] = 50,
        describe: Annotated[
            bool,
            Field(
                description="Send images to the configured AI provider for descriptions. Remote providers see the images and may charge. Default uses local OCR only."
            ),
        ] = False,
        force: Annotated[
            bool,
            Field(
                description="Reprocess completed images and replace cached results. This can repeat provider charges."
            ),
        ] = False,
        time_budget_seconds: Budget = 120,
    ) -> Result:
        """Read text from chat images and optionally request descriptions from a provider.

        Use get_media_text to read a saved result. Missing files need the bridge. Force replaces cached results. Providers may charge. Empty scans find nothing. Demo previews cached text.
        """
        c = cfg()
        if c.demo:
            cached = cached_media(chat_jids(chat), "image", since, limit)
            return ok(read=[], **cached, note=UNTRUSTED)
        engine = None
        notes: list[str] = []
        try:
            engine = vision.get_engine(c)
        except vision.OcrUnavailable as exc:
            if not describe:
                return fail(str(exc))
            notes.append(str(exc))
        provider = None
        if describe:
            try:
                provider = ai.get_provider(c)
            except ai.AIError as exc:
                return fail(str(exc))
        if engine is None and provider is None:
            return fail(NO_OCR)
        res = vision.read_chat(
            bridge(),
            chat_jids(chat)[0],
            engine,
            provider,
            after=since,
            limit=max(1, min(limit, 200)),
            force=force,
            budget_seconds=max(1, min(time_budget_seconds, 600)),
            cfg=c,
        )
        extra = {"notes": notes} if notes else {}
        return ok(**res, **extra, note=UNTRUSTED)
