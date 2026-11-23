import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from wa_mcp import config
from wa_mcp_server import app, embed, toolsets


def test_core_and_full_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_TOOLSETS", raising=False)
    cfg = config.load(tmp_path / "config.json")
    server = app.create_app(cfg)
    assert {t.name for t in asyncio.run(server.list_tools())} == set(toolsets.TOOLSETS["core"])
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool("delete_message", {}))
    names = [name for group in toolsets.TOOLSETS.values() for name in group]
    assert len(set(names)) == len(names)
    assert set(names) == embed.tool_names(cfg)
    assert set(names) == {t.name for t in asyncio.run(app.create_app(cfg, "all").list_tools())}


def test_toolset_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WA_MCP_TOOLSETS", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcp": {"toolsets": ["tasks"]}}), encoding="utf-8")
    cfg = config.load(path)
    assert toolsets.enabled(cfg) == ("tasks",)
    monkeypatch.setenv("WA_MCP_TOOLSETS", "core, actions,core")
    assert toolsets.enabled(cfg) == ("core", "actions")
    assert toolsets.enabled(cfg, "media") == ("media",)
    with pytest.raises(config.ConfigError, match="unknown MCP toolsets"):
        toolsets.enabled(cfg, "all,typo")


def test_legacy_discovery_and_dispatch(tmp_path: Path) -> None:
    from wa_mcp_server import demo

    cfg = demo.prepare(tmp_path)
    server = app.create_app(cfg, "tasks")
    listed = {tool.name for tool in asyncio.run(server.list_tools())}
    assert listed == set(toolsets.TOOLSETS["tasks"])
    assert not cfg.insight_db.exists()
    for name in ("find_task_for_message", "which_task"):
        _, data = asyncio.run(server.call_tool(name, {"message_id": "missing"}))
        assert data["found"] is False
    assert not cfg.insight_db.exists()
    _, detail = asyncio.run(server.call_tool("task_detail", {"query": "invoice"}))
    assert detail["found"] and detail["steps"]
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool("bridge_status", {}))

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcp": {"legacy_tool_names": True}}), encoding="utf-8")
    catalog = {tool.name: tool for tool in asyncio.run(app.create_app(config.load(path), "all").list_tools())}
    assert set(catalog) == set(toolsets.LEGACY_NAMES) | {
        name for group in toolsets.TOOLSETS.values() for name in group
    }
    for old, new in toolsets.LEGACY_NAMES.items():
        assert catalog[old].description.startswith(f"Deprecated alias for {new}.")
        assert catalog[old].outputSchema == catalog[new].outputSchema
    assert catalog["get_task"].inputSchema["required"] == ["task_id"]
    assert "query" in catalog["task_detail"].inputSchema["properties"]
