"""Tool: the WhatsApp accounts this server can use."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import accounts
from wa_mcp_server.common import Result, hints, ok, safe


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("List WhatsApp accounts", read_only=True))
    @safe
    def list_accounts(
        account: Annotated[
            str | None, Field(description="Configured name, such as work. Omit to list every account.")
        ] = None,
    ) -> Result:
        """Discover the configured WhatsApp accounts and query their bridges for connection status. Returns
        count, default and accounts with name, masked number, store, linked and connected. Use
        bridge_status for diagnostics on one account.
        """
        names = [account] if account else list(accounts.names())
        found = [accounts.status(n) for n in names]
        return ok(accounts=found, count=len(found), default=accounts.DEFAULT)
