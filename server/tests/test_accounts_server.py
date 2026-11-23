"""Two accounts through the real stdio server: each tool call goes to the store and bridge of its account."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wa_mcp_server.app import ACCOUNTS_NOTE

TOKEN = "test-token-0123456789"
T0_DAY = datetime.fromtimestamp(synth.T0).date().isoformat()


def fake_bridge(number: str) -> tuple[ThreadingHTTPServer, list[tuple[str, str, Any]]]:
    """A bridge API on a free port that records requests and reports `number` as its account."""
    seen: list[tuple[str, str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, data: dict[str, Any]) -> None:
            out = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def _handle(self, method: str) -> None:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._reply(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            path = self.path.split("?", 1)[0].removeprefix("/api/v1")
            seen.append((method, path, body))
            if path == "/status":
                self._reply(
                    200, {"ok": True, "connected": True, "logged_in": True, "account": number, "version": "t"}
                )
            elif path == "/send":
                self._reply(
                    200, {"ok": True, "id": "SENT-" + number[-4:], "sent_at": synth.T0, "waited_ms": 0}
                )
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("accounts")
    home, home_seen = fake_bridge("+1 *** *** 0001")
    work, work_seen = fake_bridge("+1 *** *** 0009")
    cfg = synth.make_config(
        root,
        port=home.server_address[1],
        sending={"enabled": False},
        accounts={
            "work": {
                "api": {"listen": f"127.0.0.1:{work.server_address[1]}"},
                "sending": {"enabled": True, "require_confirm": True, "allow_confirm_flag": True},
            }
        },
    )
    synth.build_wa_db(root / "store" / "wa.db")
    synth.build_session_db(root / "store" / "session.db")
    synth.build_work_store(root / "store" / "work")
    yield {"root": root, "cfg": cfg, "home": home_seen, "work": work_seen}
    home.shutdown()
    work.shutdown()


def _command() -> tuple[str, list[str]]:
    exe = shutil.which("wa-mcp", path=str(Path(sys.executable).parent))
    return (exe, []) if exe else (sys.executable, ["-m", "wa_mcp_server"])


async def _session(cfg: Path, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    command, args = _command()
    environ = dict(os.environ, WA_MCP_CONFIG=str(cfg))
    environ.pop("WA_MCP_SENDING_ENABLED", None)
    params = StdioServerParameters(command=command, args=args, env=environ)
    results: list[dict[str, Any]] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        tools = (await session.list_tools()).tools
        for name, arguments in calls:
            res = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=90))
            results.append(json.loads("".join(getattr(c, "text", "") for c in res.content)))
    return {"instructions": init.instructions or "", "tools": tools, "results": results}


@pytest.fixture(scope="module")
def run(env: dict[str, Any]) -> dict[str, Any]:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_accounts", {}),
        ("list_messages", {"query": "quarterly"}),
        ("list_messages", {"query": "quarterly", "account": "work"}),
        ("get_chat", {"chat": "Dana", "account": "work"}),
        ("get_chat", {"chat": "Dana"}),
        ("bridge_status", {"account": "work"}),
        ("bridge_status", {}),
        ("send_message", {"to": synth.DANA, "text": "On it", "confirm": True}),
        ("send_message", {"to": synth.DANA, "text": "On it", "confirm": True, "account": "work"}),
        ("list_chats", {"account": "club"}),
        ("my_day", {"date": T0_DAY}),
        ("my_day", {"date": T0_DAY, "account": "work"}),
        ("list_accounts", {"account": "work"}),
    ]
    return asyncio.run(_session(env["cfg"], calls))


def test_every_tool_takes_an_account(run: dict[str, Any]) -> None:
    tools = {t.name: t for t in run["tools"]}
    assert "list_accounts" in tools
    missing = sorted(
        n for n, t in tools.items() if "account" not in (t.inputSchema or {}).get("properties", {})
    )
    assert not missing, missing
    prop = tools["list_messages"].inputSchema["properties"]["account"]
    assert "list_accounts" in prop["description"] and "account" not in tools["list_messages"].inputSchema.get(
        "required", []
    )
    assert ACCOUNTS_NOTE.strip() in run["instructions"]


def test_list_accounts(run: dict[str, Any]) -> None:
    res = run["results"][0]
    assert res["ok"] and res["count"] == 2 and res["default"] == "default"
    home, work = res["accounts"]
    assert home["name"] == "default" and home["store"] == "store" and home["number"] == "+1 *** *** 0001"
    assert work["name"] == "work" and work["store"] == "store/work" and work["number"] == "+1 *** *** 0009"
    for a in (home, work):
        assert a["linked"] is True and a["bridge_running"] is True and a["connected"] is True
    assert home["sending_enabled"] is False and work["sending_enabled"] is True
    only = run["results"][12]
    assert only["count"] == 1 and only["accounts"][0]["name"] == "work"


def test_reads_go_to_the_right_store(run: dict[str, Any]) -> None:
    r = run["results"]
    assert r[1]["ok"] and r[1]["messages"] == []
    assert [m["id"] for m in r[2]["messages"]] == ["W1"]
    assert r[3]["ok"] and r[3]["chat"]["jid"] == synth.DANA and r[3]["chat"]["name"] == "Dana Work"
    assert r[4]["ok"] is False and "Dana" in r[4]["error"]


def test_stdio_reports_optional_speech_backend(run: dict[str, Any]) -> None:
    status = run["results"][6]
    assert status["transcription_backend"] == "faster-whisper"
    assert status["transcription_ready"] == (importlib.util.find_spec("faster_whisper") is not None)


def test_bridge_calls_go_to_the_right_bridge(run: dict[str, Any], env: dict[str, Any]) -> None:
    r = run["results"]
    assert r[5]["bridge"]["account"] == "+1 *** *** 0009" and r[5]["sending_enabled"] is True
    assert r[6]["bridge"]["account"] == "+1 *** *** 0001" and r[6]["sending_enabled"] is False
    # sending settings are per account: off for default, on for work
    assert r[7]["ok"] is False and "disabled" in r[7]["error"]
    assert r[8]["ok"] and r[8]["id"] == "SENT-0009" and r[8]["to_name"] == "Dana Work"
    assert [p for _, p, _ in env["home"] if p == "/send"] == []
    work_sends = [b for _, p, b in env["work"] if p == "/send"]
    assert len(work_sends) == 1 and work_sends[0]["to"] == synth.DANA and work_sends[0]["text"] == "On it"


def test_unknown_account(run: dict[str, Any]) -> None:
    res = run["results"][9]
    assert res["ok"] is False and 'account "club" is not configured (accounts: default, work)' in res["error"]


def test_my_day_covers_all_accounts(run: dict[str, Any], env: dict[str, Any]) -> None:
    merged, work = run["results"][10], run["results"][11]
    assert merged["ok"] and merged["accounts"] == ["default", "work"]
    assert {g["account"] for g in merged["people"]} <= {"default", "work"}
    assert all(it["account"] in ("default", "work") for g in merged["people"] for it in g["items"])
    assert work["ok"] and work["accounts"] == ["work"]
    assert all(g["account"] == "work" for g in work["people"])
    assert (env["root"] / "store" / "work" / "insight.db").is_file()


def test_approval_prompt_names_the_account(env: dict[str, Any]) -> None:
    from wa_mcp import config, db
    from wa_mcp_server import common
    from wa_mcp_server.tools import send

    common.setup(config.load(env["cfg"]))
    with db.use_account("work"):
        checked = send.plan(synth.DANA, text="On it")
    assert checked.lines[0] == "From account: work" and checked.lines[1].startswith("To: Dana Work")


def test_single_account_output_has_no_account_fields(tmp_path: Path) -> None:
    cfg = synth.make_config(tmp_path, port=1)
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    synth.build_session_db(tmp_path / "store" / "session.db")
    out = asyncio.run(_session(cfg, [("my_day", {"date": T0_DAY}), ("list_accounts", {})]))
    day, listing = out["results"]
    assert day["ok"] and "accounts" not in day
    assert all("account" not in g for g in day["people"])
    assert listing["count"] == 1 and listing["accounts"][0]["name"] == "default"
    assert listing["accounts"][0]["bridge_running"] is False
    assert ACCOUNTS_NOTE.strip() not in out["instructions"]
