"""register(): chosen wa-mcp tools inside another FastMCP server, in process and over stdio."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP

from wa_mcp import config
from wa_mcp_server.embed import register, tool_names


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    path = synth.make_config(tmp_path)
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    synth.build_session_db(tmp_path / "store" / "session.db")
    return path


def test_register_picks_and_renames(cfg_path: Path) -> None:
    host = FastMCP("host")

    @host.tool()
    def list_chats() -> str:
        """The host's own tool of the same name."""
        return "host"

    added = register(
        host,
        include=["list_calls", "get_bridge_status", "list_chats"],
        cfg=config.load(cfg_path),
        rename={"list_chats": "wa_list_chats"},
    )
    assert set(added) == {"wa_list_chats", "list_calls", "get_bridge_status"}
    names = {t.name for t in asyncio.run(host.list_tools())}
    assert names == {"list_chats", "wa_list_chats", "list_calls", "get_bridge_status"}
    assert "send_message" in tool_names(config.load(cfg_path))
    with pytest.raises(ValueError, match="no_such_tool"):
        register(FastMCP("x"), include=["no_such_tool"], cfg=config.load(cfg_path))


def test_registered_tools_over_stdio(cfg_path: Path, tmp_path: Path) -> None:
    script = tmp_path / "host.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            from mcp.server.fastmcp import FastMCP
            from wa_mcp import config
            from wa_mcp_server.embed import register

            mcp = FastMCP("host")
            register(mcp, include=["list_calls", "list_messages"], cfg=config.load(sys.argv[1]))
            mcp.run("stdio")
            """
        ),
        encoding="utf-8",
    )

    async def session() -> tuple[set[str], dict[str, object]]:
        params = StdioServerParameters(
            command=sys.executable, args=[str(script), str(cfg_path)], env=dict(os.environ)
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            res = await s.call_tool("list_messages", {"limit": 2})
            text = "".join(getattr(c, "text", "") for c in res.content)
            return names, json.loads(text)

    names, out = asyncio.run(session())
    assert names == {"list_calls", "list_messages"}
    assert out["ok"] is True
    assert len(out["messages"]) == 2


def test_session_file_in_python_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"session_file": "whatsapp.db"}', encoding="utf-8")
    assert config.load(path).session_db == tmp_path / "store" / "whatsapp.db"
    assert config.load(tmp_path / "missing.json").session_db.name == "session.db"
    for bad in ("../x.db", "sub/x.db", "wa.db"):
        path.write_text(json.dumps({"session_file": bad}), encoding="utf-8")
        with pytest.raises(config.ConfigError, match="session_file"):
            config.load(path)
