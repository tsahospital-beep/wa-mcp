"""Approve an action on a specific stored WhatsApp message."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from wa_mcp_server.common import (
    Result,
    blocking,
    bridge,
    cfg,
    clean_label,
    find_message,
    hints,
    labeled_name,
    safe,
)
from wa_mcp_server.tools import send
from wa_mcp_server.tools.inputs import Chat, Confirm, MessageId, Recipient

Action = Literal["react", "edit", "delete", "forward"]


def _preview(action: Action, chat: str, message_id: str, value: str, to: str | None) -> tuple[str, send.Plan]:
    if not cfg().sending.enabled:
        raise ValueError("sending is disabled: set sending.enabled to true in config.json")
    if message_id.startswith("imp-"):
        raise ValueError("select a synced WhatsApp message")
    row = find_message(chat if action == "forward" else send.recipient(chat), message_id)
    if row.get("deleted_at"):
        raise ValueError("message was deleted")
    if action in ("edit", "delete") and not row["outgoing"]:
        raise ValueError("only your own messages can be edited or deleted")
    if action in ("edit", "forward") and row["kind"] != "text":
        raise ValueError("select a text message")
    if action == "edit" and not value.strip():
        raise ValueError("replacement text is empty")
    if action == "react" and (len(value.encode()) > 32 or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("give a reaction or an empty string to remove it")
    source = str(row["conv"])
    body = str(row["body"] or "")
    content = body if action == "forward" else value
    if len(content) > send.MAX_TEXT or (action == "forward" and not content.strip()):
        raise ValueError("select nonblank text of up to 4096 characters")
    target = send.recipient(to) if to is not None else source
    lines = [
        f"To: {labeled_name(target)}",
        f"Source: {labeled_name(source)}, message {clean_label(message_id)}",
    ]
    if len(cfg().account_names) > 1:
        lines.insert(0, f"From account: {cfg().account}")
    if action != "forward":
        lines.append("Selected message: " + clean_label(body or str(row["kind"]), 200))
    return source, send.Plan(target, lines, content)


async def _act(
    action: Action,
    chat: str,
    message_id: str,
    ctx: Context[Any, Any, Any],
    confirm: bool,
    value: str = "",
    to: str | None = None,
) -> Result:
    source, checked = await blocking(_preview, action, chat, message_id, value, to)
    prompts = {
        "react": "Set this reaction?" if value else "Remove your reaction?",
        "edit": "Replace the selected message with this text?",
        "delete": "Delete this message for everyone?",
        "forward": "Forward this text to the recipient?",
    }
    refused = await send.approve(ctx, prompts[action], checked, confirm)
    if refused:
        return refused
    client = bridge()
    if action == "react":
        return await blocking(client.react_to_message, source, message_id, value)
    if action == "edit":
        return await blocking(client.edit_message, source, message_id, value)
    if action == "delete":
        return await blocking(client.delete_message, source, message_id)
    return await blocking(
        client.forward_message, source, message_id, checked.target, expected_text=checked.text
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Set a message reaction", destructive=True, open_world=True))
    @safe
    async def react_to_message(
        chat: Recipient,
        message_id: MessageId,
        emoji: Annotated[
            str,
            Field(
                description="Reaction symbol, up to 32 UTF-8 bytes. Pass an empty string to remove your reaction."
            ),
        ],
        ctx: Context[Any, Any, Any],
        confirm: Confirm = False,
    ) -> Result:
        """Set or remove your reaction to one synced message. Use list_messages to find its id.
        Requires sending approval under the configured policy. Returns id and sent_at, or queued and scheduled_id."""
        return await _act("react", chat, message_id, ctx, confirm, emoji)

    @mcp.tool(annotations=hints("Edit your message", destructive=True, open_world=True))
    @safe
    async def edit_message(
        chat: Recipient,
        message_id: MessageId,
        text: Annotated[str, Field(description="Full replacement text, 1 to 4096 characters.")],
        ctx: Context[Any, Any, Any],
        confirm: Confirm = False,
    ) -> Result:
        """Replace the text of a message sent by this account. Use delete_message to revoke it.
        Requires sending approval under the configured policy. Returns id and sent_at, or queued and scheduled_id."""
        return await _act("edit", chat, message_id, ctx, confirm, text)

    @mcp.tool(annotations=hints("Delete your message for everyone", destructive=True, open_world=True))
    @safe
    async def delete_message(
        chat: Recipient, message_id: MessageId, ctx: Context[Any, Any, Any], confirm: Confirm = False
    ) -> Result:
        """Revoke one message sent by this account for everyone in the chat. Use edit_message to correct text.
        Requires sending approval under the configured policy. Returns id and sent_at, or queued and scheduled_id."""
        return await _act("delete", chat, message_id, ctx, confirm)

    @mcp.tool(annotations=hints("Forward a text message", destructive=True, open_world=True))
    @safe
    async def forward_message(
        chat: Chat,
        message_id: MessageId,
        to: Recipient,
        ctx: Context[Any, Any, Any],
        confirm: Confirm = False,
    ) -> Result:
        """Forward one synced text message to a recipient with WhatsApp's forwarded marker. Use send_file for a local attachment.
        Approval covers the destination and exact text. Returns id and sent_at, or queued and scheduled_id."""
        return await _act("forward", chat, message_id, ctx, confirm, to=to)
