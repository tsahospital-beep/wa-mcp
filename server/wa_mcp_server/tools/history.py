"""Tools: fetch older messages from the phone."""

from mcp.server.fastmcp import FastMCP

from wa_mcp import history
from wa_mcp_server.common import FETCH, Result, bridge, chat_jids, ok, safe

MAX_BUDGET_SECONDS = 600


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=FETCH)
    @safe
    def request_chat_history(chat: str, count: int = 50) -> Result:
        """Ask our own phone once for up to 100 messages older than the oldest stored one in this chat.

        Nothing is sent to the contact. Messages arrive in the background within seconds.
        """
        res = history.request_history(bridge(), chat_jids(chat)[0], count=max(1, min(count, 100)))
        res.pop("http_status", None)
        return ok(**{k: v for k, v in res.items() if k != "ok"})

    @mcp.tool(annotations=FETCH)
    @safe
    def sync_chat_history(
        chat: str,
        back_to: str,
        max_pages: int = 10,
        wait_seconds: float = 20.0,
        time_budget_seconds: int = 120,
    ) -> Result:
        """Page back through one chat until messages from back_to (YYYY-MM-DD) are stored.

        Stops at the page cap (max 50), when the phone sends nothing older within wait_seconds per page, or
        after time_budget_seconds (max 600). Call again to continue.
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

    @mcp.tool(annotations=FETCH)
    @safe
    def sync_all_history(
        active_since: str,
        back_to: str,
        max_pages_per_chat: int = 5,
        max_chats: int = 50,
        restart: bool = False,
        wait_seconds: float = 20.0,
        time_budget_seconds: int = 120,
    ) -> Result:
        """Run sync_chat_history for every chat active since active_since, back to back_to.

        Each call stops after time_budget_seconds (max 600) and returns complete=false with a resume hint;
        progress is saved, so calling it again continues with the chats that did not reach back_to yet.
        restart=true forgets the saved progress.
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
