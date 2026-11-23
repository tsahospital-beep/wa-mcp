"""Tools: attachments, stickers, document text and shared Google docs."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import db, files, links, media
from wa_mcp_server import outputs
from wa_mcp_server.common import (
    UNTRUSTED,
    Result,
    bridge,
    cfg,
    chat_jids,
    clamp,
    fail,
    find_message,
    hints,
    ok,
    shape_messages,
    structured,
)
from wa_mcp_server.tools.inputs import After, Before, Chat, ChatFilter, MessageId, Offset, PageSize


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Download one attachment", idempotent=True, open_world=True))
    @structured(outputs.Output[outputs.Download])
    def download_media(chat: Chat, message_id: MessageId) -> Result:
        """Save the attachment from one message, reusing a verified local copy.

        Use download_chat_media for a batch. A missing message fails. New downloads need the bridge. Demo declines downloads.
        """
        row = find_message(chat, message_id)
        res = media.download_media(bridge(), str(row["conv"]), message_id)
        return ok(**res, note=UNTRUSTED)

    @mcp.tool(annotations=hints("Download attachments from a chat", idempotent=True, open_world=True))
    @structured(outputs.Output[outputs.Downloads])
    def download_chat_media(
        chat: Chat,
        after: After = None,
        before: Before = None,
        kinds: Annotated[
            list[str] | None,
            Field(
                description='Attachment types to save, such as ["image","document"]. Choices are image, video, ptv, audio, voice, document and sticker. Omit for all.'
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description="Number of newest matching attachment messages to inspect, up to 200. Zero or negative uses 50."
            ),
        ] = 50,
    ) -> Result:
        """Save matching attachments from one chat and reuse verified local copies.

        Use download_media for one message. No matches gives an empty batch. Missing files need the bridge and can fail individually. Demo refuses downloads.
        """
        res = media.download_chat_media(
            bridge(), chat_jids(chat)[0], after, before, kinds, limit=clamp(limit)
        )
        return ok(**res, note=UNTRUSTED)

    @mcp.tool(annotations=hints("Read stored attachment text", read_only=True))
    @structured(outputs.Output[outputs.MediaText])
    def get_media_text(chat: Chat, message_id: MessageId) -> Result:
        """Read the text already extracted from one attachment or voice note.

        Use transcribe_chat_audio to generate a transcript. Works offline and in demo. A missing message fails, and a message without extracted text gives an empty list.
        """
        row = find_message(chat, message_id)
        rows = db.derived(str(row["conv"]), message_id)
        items = [
            {"source": r["source"], "lang": r["lang"], "confidence": r["confidence"], "text": r["content"]}
            for r in rows
            if r["content"] or not str(r["source"]).startswith("image")
        ]
        extra: Result = {}
        if row["kind"] == "image" and not items:
            extra["hint"] = (
                "the image was read and holds no text"
                if rows
                else "no text yet: run read_images for this chat"
            )
        return ok(message_id=message_id, kind=row["kind"], items=items, **extra, note=UNTRUSTED)

    @mcp.tool(annotations=hints("List sticker messages", read_only=True))
    @structured(outputs.Output[outputs.Stickers])
    def list_stickers(chat: ChatFilter = None, limit: PageSize = 50, offset: Offset = 0) -> Result:
        """Browse stored sticker messages without downloading files.

        Use download_media to save a sticker. Works offline and in demo. An unknown chat name fails and no matches gives an empty page.
        """
        convs = chat_jids(chat) if chat else None
        rows = db.messages(conv=convs, kind="sticker", limit=clamp(limit), offset=max(offset, 0))
        return ok(stickers=shape_messages(rows), next_offset=offset + len(rows), note=UNTRUSTED)

    @mcp.tool(
        annotations=hints("Extract text from a document", destructive=True, idempotent=True, open_world=True)
    )
    @structured(outputs.Output[outputs.Extracted])
    def extract_file_text(chat: Chat, message_id: MessageId) -> Result:
        """Extract and store text from a document or list entries in an archive.

        Use get_media_text for a saved extraction. A missing or unsupported document fails. Uncached files need the bridge. Demo uses its local document.
        """
        row = find_message(chat, message_id)
        if row["kind"] != "document":
            return fail(f"message {message_id} is a {row['kind']}, not a document")
        if cfg().demo:
            path = media.local_file(row)
            if path is None:
                return fail("The demo document is missing. Restart the demo to recreate it.")
        else:
            path = media.ensure_local(bridge(), row)
        try:
            res = files.extract_message(str(row["conv"]), message_id, path)
        except files.FileError as exc:
            return fail(str(exc))
        return ok(message_id=message_id, path=str(path), **res.as_dict(), note=UNTRUSTED)

    @mcp.tool(annotations=hints("Fetch linked Google documents", destructive=True, open_world=True))
    @structured(outputs.Output[outputs.Documents])
    def fetch_shared_docs(
        chat: Chat,
        after: After = None,
        before: Before = None,
        limit: Annotated[
            int, Field(description="Maximum document links to attempt, clamped to 1 through 50.")
        ] = 10,
    ) -> Result:
        """Download text from public Google documents linked in a chat and store it with the messages.

        Use check_links for availability only. Works without the bridge. Google sees the host IP. No links gives an empty batch. Private or offline demo links fail individually.
        """
        res = links.fetch_shared_docs(
            chat_jids(chat)[0], after=after, before=before, limit=max(1, min(limit, 50))
        )
        return ok(**res, note=UNTRUSTED)

    @mcp.tool(annotations=hints("Check shared link availability", read_only=True, open_world=True))
    @structured(outputs.Output[outputs.Links])
    def check_links(
        chat: Chat,
        after: After = None,
        before: Before = None,
        limit: Annotated[
            int, Field(description="Maximum distinct URLs to check, clamped to 1 through 50.")
        ] = 10,
    ) -> Result:
        """Check whether public HTTPS links in stored messages are reachable.

        Use fetch_shared_docs to save document text. Works without the bridge. Sites see the host IP. No links gives an empty list. Demo reports links as offline.
        """
        res = links.check_chat_links(
            chat_jids(chat)[0], after=after, before=before, limit=max(1, min(limit, 50))
        )
        return ok(**res, note=UNTRUSTED)
