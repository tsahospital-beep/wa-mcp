"""get_bridge_status through the real MCP stdio client shows session security events and protection."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN = "test-token-0123456789"
EVENT = {
    "at": synth.T0,
    "kind": "session_replaced",
    "level": "warning",
    "detail": "another client connected with the keys of this session",
}


class StatusBridge(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        ok = self.headers.get("Authorization") == f"Bearer {TOKEN}" and self.path.startswith("/api/v1/status")
        body = {
            "ok": ok,
            "connected": True,
            "logged_in": True,
            "account": "+1 *** 0101",
            "version": "t",
            "security_events": [EVENT],
            "security_alert": True,
            "protection": {
                "field_encryption": "keystore",
                "key_store": "dpapi",
                "api_token": "dpapi",
                "efs": "on",
            },
        }
        out = json.dumps(body if ok else {"ok": False, "error": "unauthorized"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def fake_bridge() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StatusBridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield int(server.server_address[1])
    server.shutdown()


async def _status(cfg: Path) -> dict[str, Any]:
    scripts = Path(sys.executable).parent
    exe = shutil.which("wa-mcp", path=str(scripts))
    command, args = (exe, []) if exe else (sys.executable, ["-m", "wa_mcp_server"])
    env = dict(os.environ, WA_MCP_CONFIG=str(cfg))
    for name in ("WA_MCP_ENCRYPTION_MODE", "WA_MCP_API_TOKEN"):
        env.pop(name, None)
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("get_bridge_status", {})
        data: dict[str, Any] = json.loads("".join(getattr(c, "text", "") for c in res.content))
        return data


def test_bridge_status_reports_security_events(tmp_path: Path, fake_bridge: int) -> None:
    cfg = synth.make_config(tmp_path, port=fake_bridge)
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    data = asyncio.run(_status(cfg))
    assert data["encryption_mode"] == "keystore" and data["encryption"] is True
    assert data["security_alert"] is True
    assert data["security_events"] == [EVENT]
    assert data["protection"]["key_store"] == "dpapi"
