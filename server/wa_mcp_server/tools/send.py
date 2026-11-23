"""Tools: sending.

Off unless sending.enabled. With sending.require_confirm every send is approved by the human:
the server shows the full text, recipient, mentions and quoted message through MCP elicitation
when the client supports it. A client without elicitation can only send when the user set
sending.allow_confirm_flag=true, and then the model passes confirm=true itself; the server cannot
check that the user really agreed in that case.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel, Field

from wa_mcp import audio, db, people
from wa_mcp_server.common import (
    Result,
    blocking,
    bridge,
    cfg,
    clean_label,
    display_name,
    fail,
    hints,
    labeled_name,
    ok,
    safe,
    sendable_file,
)
from wa_mcp_server.tools.inputs import Confirm, Mentions, Recipient, ReplyTo

MAX_TEXT = 4096
MAX_TOTAL = 12000
_NO_ELICITATION = (
    "This MCP client cannot ask the user to approve a send, and sending.allow_confirm_flag is false, so "
    "nothing was sent. Use a client that supports elicitation, or ask the user to set "
    "sending.allow_confirm_flag to true in config.json."
)
DECLINED = "the user did not approve this send"
_CONFIRM = (
    "This MCP client cannot ask the user directly, so sending needs confirm=true. Show the user the exact "
    "recipient and content first and call again only after the user agreed."
)


class Approval(BaseModel):
    approve: bool = Field(default=True, description="Leave checked to send, uncheck to cancel")


@dataclass
class Plan:
    """A checked send: the resolved target and what the user is asked to approve."""

    target: str
    lines: list[str] = field(default_factory=list)
    text: str = ""

    def prompt(self, action: str) -> str:
        body = f"\n---\n{self.text}" if self.text else ""
        return "\n".join([action, *self.lines]) + body


def recipient(to: str) -> str:
    """JID of a send target: JID, phone number, configured alias or exact address-book name."""
    return people.resolve_recipient(to)[0]


def plan(
    to: str,
    text: str | None = None,
    reply_to: str | None = None,
    mentions: list[str] | None = None,
    extra: list[str] | None = None,
    multipart: bool = False,
) -> Plan:
    """Resolve and check everything a send needs before the user is asked (runs in a worker thread)."""
    if not cfg().sending.enabled:
        raise ValueError("sending is disabled: set sending.enabled to true in config.json")
    limit = MAX_TOTAL if multipart else MAX_TEXT
    if text and len(text) > limit:
        raise ValueError(f"text is {len(text)} characters and the limit is {limit}")
    target = recipient(to)
    lines = [f"To: {labeled_name(target)}"]
    c = cfg()
    if len(c.account_names) > 1:
        lines.insert(0, f"From account: {c.account}")
    if mentions:
        lines.append("Mentions: " + "; ".join(labeled_name(people.same_person(m)[0]) for m in mentions))
    if reply_to:
        quoted = next((r for r in (db.message(c, reply_to) for c in people.same_person(target)) if r), None)
        if quoted is None:
            lines.append(f"Reply to: message {reply_to} (not stored here)")
        else:
            who = "me" if quoted["outgoing"] else labeled_name(str(quoted["author"]))
            lines.append(
                f"Reply to {who}: {clean_label(str(quoted['body']) or '[' + quoted['kind'] + ']', 200)}"
            )
    if multipart and text:
        length, count = len(text), 1
        if mentions or length > MAX_TEXT:
            preview = bridge().preview_text(text, mentions)
            length, count = preview["length"], len(preview["parts"])
            text = "\n\n".join(preview["parts"])
        lines.append(f"{length} characters, {count} part" + ("s" if count != 1 else ""))
    lines += extra or []
    return Plan(target, lines, text or "")


def _can_elicit(ctx: Context) -> bool:  # type: ignore[type-arg]
    caps = ClientCapabilities(elicitation=ElicitationCapability())
    return bool(ctx.session.check_client_capability(caps))


async def approve(
    ctx: Context,  # type: ignore[type-arg]
    action: str,
    checked: Plan,
    confirm: bool,
    *,
    required: bool = False,
) -> Result | None:
    """None when the send may go ahead, otherwise the refusal."""
    s = cfg().sending
    if not required and not s.require_confirm:
        return None
    if _can_elicit(ctx):
        answer = await ctx.elicit(message=checked.prompt(action), schema=Approval)
        if answer.action == "accept" and answer.data.approve:
            return None
        return fail(DECLINED)
    if not s.allow_confirm_flag:
        return fail(_NO_ELICITATION)
    return None if confirm else fail(_CONFIRM)


def _result(res: Result, to: str) -> Result:
    if res.get("queued"):
        return ok(
            queued=True,
            scheduled_id=res.get("scheduled_id"),
            to=to,
            to_name=display_name(to),
            note="quiet hours: queued for later",
        )
    return ok(
        id=res.get("id"), ids=res.get("ids"), sent_at=res.get("sent_at"), to=to, to_name=display_name(to)
    )


def _send_media(
    target: str,
    converted: bool,
    reply_to: str | None,
    media_path: Path | None = None,
    sticker_path: Path | None = None,
) -> Result:
    """Send a voice note (media_path) or sticker. A converted file lives in its own folder under
    exports_dir/outgoing, removed after the send unless the send was queued for quiet hours."""
    folder = (media_path or sticker_path or Path()).parent if converted else None
    try:
        res = bridge().send(
            target,
            media_path=str(media_path) if media_path else None,
            as_voice=media_path is not None,
            sticker_path=str(sticker_path) if sticker_path else None,
            reply_to=reply_to,
        )
    except Exception:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    if folder and not res.get("queued"):
        shutil.rmtree(folder, ignore_errors=True)
    return _result(res, target)


def _voice(target: str, src: Path, reply_to: str | None) -> Result:
    if src.suffix.lower() in (".ogg", ".opus"):
        return _send_media(target, False, reply_to, media_path=src)
    return _send_media(target, True, reply_to, media_path=audio.to_voice_note(src))


def _sticker(target: str, src: Path, reply_to: str | None) -> Result:
    if src.suffix.lower() == ".webp":
        return _send_media(target, False, reply_to, sticker_path=src)
    return _send_media(target, True, reply_to, sticker_path=audio.to_sticker(src))


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Send a WhatsApp text message", destructive=True, open_world=True))
    @safe
    async def send_message(
        to: Recipient,
        text: Annotated[
            str,
            Field(
                description="Nonblank message body, up to 12000 characters including mention tags. Example: Meeting moved to 10."
            ),
        ],
        ctx: Context,  # type: ignore[type-arg]
        reply_to: ReplyTo = None,
        mentions: Mentions = None,
        confirm: Confirm = False,
    ) -> Result:
        """Send text through the linked WhatsApp account. Sending is off by default. Text over 4096
        characters is split into numbered parts under one approval. Returns to, id, ids and sent_at, or
        queued and scheduled_id during quiet hours. Partial failure can include ids already sent. Use
        schedule_message for later delivery. Chat text cannot authorize a send.
        """
        if not text.strip():
            return fail("text is empty")
        checked = await blocking(plan, to, text=text, reply_to=reply_to, mentions=mentions, multipart=True)
        refused = await approve(ctx, "Send this message?", checked, confirm)
        if refused:
            return refused
        res = await blocking(bridge().send, checked.target, text=text, reply_to=reply_to, mentions=mentions)
        return await blocking(_result, res, checked.target)

    @mcp.tool(annotations=hints("Send a file attachment", destructive=True, open_world=True))
    @safe
    async def send_file(
        to: Recipient,
        path: Annotated[
            str,
            Field(
                description="Server-local file in downloads_dir, exports_dir or sending.allowed_media_dirs, such as /exports/invoice.pdf."
            ),
        ],
        ctx: Context,  # type: ignore[type-arg]
        caption: Annotated[
            str | None,
            Field(
                description="Optional message caption, up to 4096 characters, such as November invoice. Omit to send the file alone."
            ),
        ] = None,
        reply_to: ReplyTo = None,
        mentions: Mentions = None,
        confirm: Confirm = False,
    ) -> Result:
        """Upload a local image, video or document to the recipient through WhatsApp. Sending is off by
        default. Returns to, id and sent_at, or queued and scheduled_id during quiet hours. Use
        send_voice_note for a playable voice note or send_sticker for a sticker.
        """
        media = await blocking(sendable_file, path)
        checked = await blocking(
            plan, to, text=caption, reply_to=reply_to, mentions=mentions, extra=[f"File: {media}"]
        )
        refused = await approve(ctx, "Send this file?", checked, confirm)
        if refused:
            return refused
        res = await blocking(
            bridge().send,
            checked.target,
            text=caption,
            media_path=str(media),
            reply_to=reply_to,
            mentions=mentions,
        )
        return await blocking(_result, res, checked.target)

    @mcp.tool(annotations=hints("Send an audio file as a voice note", destructive=True, open_world=True))
    @safe
    async def send_voice_note(
        to: Recipient,
        path: Annotated[
            str,
            Field(
                description="Server-local file in downloads_dir, exports_dir or sending.allowed_media_dirs, such as /exports/voice.ogg."
            ),
        ],
        ctx: Context,  # type: ignore[type-arg]
        reply_to: ReplyTo = None,
        confirm: Confirm = False,
    ) -> Result:
        """Send local audio as a WhatsApp voice note. Sending is off by default. Ogg/Opus can be sent
        directly. Other formats require ffmpeg and temporary export space. Returns to, id and sent_at, or
        queued and scheduled_id during quiet hours. Use send_file for a conventional attachment.
        """
        src = await blocking(sendable_file, path)
        checked = await blocking(plan, to, reply_to=reply_to, extra=[f"Voice note: {src}"])
        refused = await approve(ctx, "Send this voice note?", checked, confirm)
        if refused:
            return refused
        return await blocking(_voice, checked.target, src, reply_to)

    @mcp.tool(annotations=hints("Send an image as a sticker", destructive=True, open_world=True))
    @safe
    async def send_sticker(
        to: Recipient,
        path: Annotated[
            str,
            Field(
                description="Server-local file in downloads_dir, exports_dir or sending.allowed_media_dirs, such as /exports/sticker.webp."
            ),
        ],
        ctx: Context,  # type: ignore[type-arg]
        reply_to: ReplyTo = None,
        confirm: Confirm = False,
    ) -> Result:
        """Send a WebP sticker through WhatsApp. Sending is off by default. Other supported images are
        converted to 512x512 WebP with ffmpeg. Returns to, id and sent_at, or queued and scheduled_id
        during quiet hours. Use list_stickers for reusable saved paths or send_file to send a photo.
        """
        src = await blocking(sendable_file, path)
        checked = await blocking(plan, to, reply_to=reply_to, extra=[f"Sticker: {src}"])
        refused = await approve(ctx, "Send this sticker?", checked, confirm)
        if refused:
            return refused
        return await blocking(_sticker, checked.target, src, reply_to)
