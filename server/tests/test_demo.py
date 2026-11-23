"""Exercise the demo through MCP tool registration with network and backend access forbidden."""

import asyncio
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from wa_mcp import config, keystore, transcribe, vision
from wa_mcp.client import Bridge
from wa_mcp_server import app, common, demo, toolsets


def dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())


def test_seed_is_deterministic_and_moves_with_local_date(tmp_path: Path) -> None:
    today = date(2024, 3, 1)
    first = demo.prepare(tmp_path, today)
    original = dump(first.wa_db)
    first.wa_db.unlink()
    (first.downloads_dir / "order.txt").unlink()
    again = demo.prepare(tmp_path, today)
    assert dump(again.wa_db) == original
    assert (again.downloads_dir / "order.txt").read_text(encoding="utf-8") == demo.DOCUMENT
    assert demo.prepare(tmp_path, today) == first
    for offset in (0, 1, 270):
        day = today + timedelta(days=offset)
        cfg = demo.prepare(tmp_path, day)
        with closing(sqlite3.connect(cfg.wa_db)) as conn:
            lo, hi, n = conn.execute(
                "SELECT MIN(sent_at), MAX(sent_at), COUNT(*) FROM chat_messages"
            ).fetchone()
            assert datetime.fromtimestamp(lo).date() == day - timedelta(days=29)
            assert datetime.fromtimestamp(hi).date() == day
            assert n == 297
        assert not cfg.session_db.exists()
    assert dump(first.wa_db) == original


def forbidden(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("demo reached a network, bridge, key or media backend")


def test_demo_dir_preserves_an_unrelated_database(tmp_path: Path) -> None:
    day = date(2024, 2, 29)
    cfg = demo.prepare(tmp_path, day)
    cfg.wa_db.unlink()
    with closing(sqlite3.connect(cfg.wa_db)) as conn, conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('keep')")
    before = dump(cfg.wa_db)
    with pytest.raises(config.ConfigError, match="expected a fictional demo store"):
        demo.prepare(tmp_path, day)
    assert dump(cfg.wa_db) == before


@pytest.fixture
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    for target, name in (
        (socket, "getaddrinfo"),
        (socket, "create_connection"),
        (httpx.Client, "send"),
        (Bridge, "__init__"),
        (keystore, "get"),
        (keystore, "read_key_file"),
        (transcribe, "get_backend"),
        (vision, "get_engine"),
        (config, "load"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setenv("WA_MCP_STORE_DIR", str(tmp_path / "host"))
    monkeypatch.setenv("WA_MCP_CONFIG", str(tmp_path / "host" / "config.json"))
    monkeypatch.setenv("WA_MCP_ENCRYPTION_MODE", "keyfile")
    monkeypatch.delenv("WA_MCP_TOOLSETS", raising=False)
    yield tmp_path
    assert not (tmp_path / "host").exists()


@pytest.mark.parametrize("args,environment", [(["--demo"], ""), ([], "1"), (["--demo-dir"], "")])
def test_cli_isolated_config(
    offline: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], environment: str
) -> None:
    monkeypatch.setenv("WA_MCP_DEMO", environment)
    fixed = "--demo-dir" in args
    stores = []

    def inspect(server: FastMCP, transport: str) -> None:
        assert transport == "stdio"
        assert {t.name for t in asyncio.run(server.list_tools())} == set(toolsets.TOOLSETS["core"])
        assert "fictional" in server.instructions and "blue umbrella" in server.instructions
        cfg = common.cfg()
        assert cfg.demo and not cfg.encryption.enabled and cfg.wa_db.is_file()
        stores.append(cfg.wa_db)

    monkeypatch.setattr(FastMCP, "run", inspect)
    app.main([*args, str(offline)] if fixed else args)
    assert len(stores) == 1 and stores[0].exists() == fixed


def test_every_tool_offline(offline: Path) -> None:
    cfg = demo.prepare(offline)
    server = app.create_app(cfg, "all")
    results: dict[str, Any] = {}

    def call(name: str, **args: Any) -> Any:
        _, result = asyncio.run(server.call_tool(name, args))
        results[name] = result
        return result

    assert call("bridge_status")["counts"] == {"chats": 8, "messages": 297}
    assert len(call("list_chats")["chats"]) == 8
    assert call("list_accounts")["accounts"][0]["linked"] is False
    assert call("search_contacts", query="Mira")["contacts"][0]["name"] == "Mira Vale"
    assert call("get_chat", chat=demo.PRESS)["chat"]["name"] == "Finch Paper Works"
    call("get_last_interaction", chat=demo.PRESS)
    (match,) = call("list_messages", query="blue umbrella")["messages"]
    assert (match["kind"], match["transcript"]) == ("voice", demo.TRANSCRIPT)
    assert call("get_message_context", chat=match["chat"], message_id=match["id"])["message"] == match
    edit = call("get_message_context", chat=demo.GARDEN, message_id="garden-table")["message"]
    assert edit["edited"] and edit["reactions"]["counts"]
    counts = call("my_day")["counts"]
    assert all(counts[key] for key in ("open_tasks", "waiting_for_reply", "missed_calls", "promises_due"))
    assert call("list_calls")["calls"][0]["outcome"] == "missed"
    assert call("refresh_tasks")["open"] >= 2
    assert call("tasks_overview")["count"] >= 2
    assert call("detect_tasks", chat=demo.MIRA)["detections"]
    assert call("which_task", message_id="invoice-request")["found"]
    assert call("task_detail", query="invoice")["found"]
    assert call("project_overview", project="fair")["open"]
    call("whats_new")
    assert call("find_mentions")["mentions"]
    assert call("chat_analytics", chat=demo.IRA)["sections"]
    assert len(call("chat_analytics_compare", chats=[demo.IRA, demo.MIRA])["rows"]) == 2
    period = (date.today() - timedelta(days=30)).isoformat()
    for export in (call("export_chat", chat=demo.IRA), call("export_period", after=period)):
        path = Path(export["path"])
        assert path.is_relative_to(cfg.exports_dir) and demo.TRANSCRIPT in path.read_text(encoding="utf-8")
    for chat, mid, expected in (
        (demo.IRA, "west-gate", demo.TRANSCRIPT),
        (demo.PRINT, "gate-sign", demo.SIGN),
        (demo.PRESS, "paper-order", demo.DOCUMENT),
    ):
        assert call("get_media_text", chat=chat, message_id=mid)["items"][0]["text"] == expected
    assert "60 cream sheets" in call("extract_file_text", chat=demo.PRESS, message_id="paper-order")["text"]
    assert call("read_images", chat=demo.PRINT, force=True)["cached"][0]["text"] == demo.SIGN
    assert call("transcribe_chat_audio", chat=demo.IRA, force=True)["cached"][0]["text"] == demo.TRANSCRIPT
    assert not call("list_stickers")["stickers"]
    assert not call("list_scheduled")["scheduled"]
    assert not call("agent_status")["enabled"]
    assert not call("agent_drafts")["drafts"]
    assert not call("check_links", chat=demo.IRA)["links"]
    assert not call("fetch_shared_docs", chat=demo.IRA)["fetched"]
    assert all(r["ok"] for r in results.values())

    inputs = {
        "chat": demo.IRA,
        "to": demo.MIRA,
        "message_id": "west-gate",
        "message_ids": ["west-gate"],
        "emoji": "+1",
        "text": "hello",
        "path": "missing.txt",
        "at": "tomorrow",
        "back_to": period,
        "active_since": period,
        "scheduled_id": 42,
        "draft_id": "missing",
    }
    live = {
        "react_to_message",
        "edit_message",
        "delete_message",
        "forward_message",
        "send_message",
        "send_file",
        "send_voice_note",
        "send_sticker",
        "mark_as_read",
        "request_chat_history",
        "sync_chat_history",
        "sync_all_history",
        "download_media",
        "download_chat_media",
        "schedule_message",
        "cancel_scheduled",
        "import_chat_export",
        "agent_test",
        "agent_approve_draft",
        "agent_pause",
        "agent_resume",
    }
    catalog = asyncio.run(server.list_tools())
    assert results.keys() | live == {t.name for t in catalog}
    for tool in catalog:
        if tool.name in live:
            args = {k: inputs[k] for k in tool.inputSchema.get("required", [])}
            assert call(tool.name, **args) == {"ok": False, "demo": True, "error": demo.LINK_NOTICE}
