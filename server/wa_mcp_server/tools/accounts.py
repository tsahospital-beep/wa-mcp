"""Tool: the WhatsApp accounts this server can use."""

from mcp.server.fastmcp import FastMCP

from wa_mcp import accounts
from wa_mcp_server.common import READ_ONLY, Result, ok, safe


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @safe
    def list_accounts(account: str | None = None) -> Result:
        """The WhatsApp numbers set up in config.json ("accounts"), each with its own bridge and store.

        For each: name, linked (a phone is linked), bridge_running and connected, the masked number and
        the store folder. Pass a name as `account` to any other tool to work on that number; without it
        tools use the account "default". account: show only this account.
        """
        names = [account] if account else list(accounts.names())
        found = [accounts.status(n) for n in names]
        return ok(accounts=found, count=len(found), default=accounts.DEFAULT)
