"""Tools: fetch older messages from the phone."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import history
from wa_mcp_server.common import Result, bridge, chat_jids, hints, ok, safe
from wa_mcp_server.tools.inputs import BackTo, Budget, Chat, PageWait

MAX_BUDGET_SECONDS = 600


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Request one older history page", open_world=True))
    @safe
    def request_chat_history(
        chat: Chat,
        count: Annotated[
            int,
            Field(
                description="Number of older messages to request, clamped to 1 through 100. The phone can return fewer."
            ),
        ] = 50,
    ) -> Result:
        """Ask the linked phone once for messages older than the oldest synced message in one chat. Returns
        requested, anchor_id and oldest_known immediately. Available messages arrive in the local archive
        asynchronously. Imported messages cannot anchor the request. Use sync_chat_history to wait and
        page back to a date.
        """
        jids = chat_jids(chat)
        _, anchor, source = history.oldest_anchor(jids)
        res = history.request_history(
            bridge(), source or jids[0], count=max(1, min(count, 100)), before_id=anchor
        )
        res.pop("http_status", None)
        return ok(**{k: v for k, v in res.items() if k != "ok"})

    @mcp.tool(annotations=hints("Sync one chat back to a date", open_world=True))
    @safe
    def sync_chat_history(
        chat: Chat,
        back_to: BackTo,
        max_pages: Annotated[
            int, Field(description="Page requests allowed in this call, clamped to 1 through 50.")
        ] = 10,
        wait_seconds: PageWait = 20.0,
        time_budget_seconds: Budget = 120,
    ) -> Result:
        """Request and wait for older history from the linked phone until the chat reaches back_to, three
        pages yield nothing older, or a cap is reached. Returns chat, pages, oldest_time, state (reached,
        pending or exhausted) and stopped. Repeat a pending call to continue. Use request_chat_history for
        one background request.
        """
        res = history.sync_chat_history(
            bridge(),
            chat_jids(chat)[0],
            back_to,
            max_pages=max(1, min(max_pages, 50)),
            wait_seconds=max(1.0, min(wait_seconds, 60.0)),
            budget_seconds=max(1, min(time_budget_seconds, MAX_BUDGET_SECONDS)),
        )
        return ok(**res.as_dict())

    @mcp.tool(annotations=hints("Sync older history across chats", destructive=True, open_world=True))
    @safe
    def sync_all_history(
        active_since: Annotated[
            str,
            Field(
                description="Select chats with a stored message strictly after this time. Use YYYY-MM-DD, Unix seconds as text or ISO 8601, such as 2026-11-01."
            ),
        ],
        back_to: BackTo,
        max_pages_per_chat: Annotated[
            int, Field(description="Maximum page requests per chat in this call, clamped to 1 through 50.")
        ] = 5,
        max_chats: Annotated[
            int, Field(description="Maximum pending chats to attempt this call, clamped to 1 through 500.")
        ] = 50,
        restart: Annotated[
            bool,
            Field(
                description="Clear all saved history-sync progress for this account before starting. Stored messages are retained."
            ),
        ] = False,
        wait_seconds: PageWait = 20.0,
        time_budget_seconds: Budget = 120,
    ) -> Result:
        """Request older messages from the phone for active chats and save progress per account. Returns
        chats with per-chat state, total_active, remaining and complete. If incomplete, repeat with
        restart=false to resume. Use sync_chat_history for one chat.
        """
        res = history.sync_all_history(
            bridge(),
            active_since,
            back_to,
            max_pages_per_chat=max(1, min(max_pages_per_chat, 50)),
            max_chats=max(1, min(max_chats, 500)),
            restart=restart,
            wait_seconds=max(1.0, min(wait_seconds, 60.0)),
            budget_seconds=max(1, min(time_budget_seconds, MAX_BUDGET_SECONDS)),
        )
        return ok(**res)
