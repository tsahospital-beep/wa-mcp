"""Shared MCP input types for common WhatsApp identifiers, filters and controls."""

from typing import Annotated

from pydantic import Field

Chat = Annotated[
    str,
    Field(
        description="Chat JID from list_chats (15550100001@s.whatsapp.net or 120363000000001@g.us), international phone number or chat name. Unknown names fail. Ambiguous names return candidates."
    ),
]
ChatFilter = Annotated[
    str | None,
    Field(
        description="Restrict to one chat by JID (15550100001@s.whatsapp.net or 120363000000001@g.us), international number or name. Omit for all chats. Unknown names fail. Ambiguous names return candidates."
    ),
]
After = Annotated[
    str | None,
    Field(
        description="Exclusive lower bound in Unix seconds as text, YYYY-MM-DD (local midnight) or ISO 8601, such as 2026-11-01T09:00:00Z. Omit for no lower bound."
    ),
]
Before = Annotated[
    str | None,
    Field(
        description="Exclusive upper bound in Unix seconds as text, YYYY-MM-DD (local midnight) or ISO 8601, such as 2026-12-01T00:00:00Z. Omit for no upper bound."
    ),
]
MessageId = Annotated[
    str, Field(description="Opaque id from list_messages in this chat, such as ABC123. Copy it exactly.")
]
PageSize = Annotated[
    int, Field(description="Maximum results per page, up to 200. Zero or negative uses the default.")
]
Offset = Annotated[
    int,
    Field(description="Number of rows to skip, starting at 0. Pass the previous next_offset to continue."),
]
BackTo = Annotated[
    str,
    Field(
        description="Stop when the oldest synced message reaches this time or earlier. Use YYYY-MM-DD, Unix seconds as text or ISO 8601, such as 2026-10-01."
    ),
]
Budget = Annotated[
    int, Field(description="Time budget in seconds, clamped to 1 through 600. Checked between operations.")
]
PageWait = Annotated[
    float, Field(description="Seconds to wait for older messages per request, clamped to 1 through 60.")
]
Recipient = Annotated[
    str,
    Field(
        description="Recipient JID (15550100001@s.whatsapp.net), international number, configured alias or exact address-book name. For groups use the JID, such as 120363000000001@g.us."
    ),
]
ReplyTo = Annotated[
    str | None,
    Field(
        description="Message id to quote in the recipient chat, such as ABC123 from list_messages. Omit for a standalone message."
    ),
]
Mentions = Annotated[
    list[str] | None,
    Field(
        description='Group members to mention by JID or international phone number, such as ["+15550100001"]. Omit for no mentions.'
    ),
]
Confirm = Annotated[
    bool,
    Field(
        description="Only for clients without elicitation when sending.allow_confirm_flag is enabled. Set true only after the user approves the exact recipient and content. Defaults to false."
    ),
]
IncludeMe = Annotated[
    bool,
    Field(
        description="Include messages sent by this account. By default only incoming messages are matched."
    ),
]
