"""Tool: the WhatsApp accounts this server can use."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from wa_mcp import accounts
from wa_mcp_server import outputs
from wa_mcp_server.common import Result, hints, ok, structured


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("List WhatsApp accounts", read_only=True))
    @structured(outputs.Output[outputs.Accounts])
    def list_accounts(
        account: Annotated[
            str | None, Field(description="Configured name, such as work. Omit to list every account.")
        ] = None,
    ) -> Result:
        """Identify configured accounts and check which bridges are connected.

        Use get_bridge_status for diagnostics on one account. Omit account to list all configured accounts. Unknown names fail. Offline accounts stay in the list. Demo reports an unlinked account.
        """
        names = [account] if account else list(accounts.names())
        found = [accounts.status(n) for n in names]
        return ok(accounts=found, count=len(found), default=accounts.DEFAULT)
