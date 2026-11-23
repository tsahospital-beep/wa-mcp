"""import_chat_export through the real stdio MCP client, against a fake bridge and a synthetic store."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wa_mcp import config
from wa_mcp_server import common

TOKEN = "test-token-0123456789"
REPORT = {
    "ok": True,
    "dry_run": False,
    "chat": synth.ALICE,
    "messages_added": 5,
    "duplicates": 0,
    "media_added": 1,
    "skipped_lines": {"count": 0, "first": []},
    "senders": [{"name": "Alice", "messages": 3, "author": synth.ALICE, "outgoing": False}],
}


class FakeBridge(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def _reply(self, status: int, data: dict[str, Any]) -> None:
        out = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._reply(401, {"ok": False, "error": "unauthorized"})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeBridge.requests.append({"path": self.path, "body": body})
        if self.path != "/api/v1/import":
            self._reply(404, {"ok": False, "error": "not found"})
        elif body.get("me") == "Nobody":
            self._reply(400, {"ok": False, "error": "no sender is called", "senders": ["Alice", "Bob"]})
        elif body.get("chat") == synth.GROUP:
            self._reply(
                404,
                {
                    "ok": False,
                    "error": "no stored chat is titled",
                    "candidates": [{"jid": synth.GROUP, "title": "Book club"}],
                },
            )
        else:
            self._reply(200, {**REPORT, "dry_run": bool(body.get("dry_run"))})

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("import-mcp")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    inbox = root / "inbox"
    inbox.mkdir()
    cfg = synth.make_config(root, port=server.server_address[1], **{"import": {"allowed_dirs": ["inbox"]}})
    synth.build_wa_db(root / "store" / "wa.db")
    synth.build_session_db(root / "store" / "session.db")
    yield {"root": root, "cfg": cfg, "inbox": inbox}
    server.shutdown()


def _command() -> tuple[str, list[str]]:
    scripts = Path(sys.executable).parent
    exe = shutil.which("wa-mcp", path=str(scripts))
    return (exe, []) if exe else (sys.executable, ["-m", "wa_mcp_server"])


async def _session(
    cfg: Path, calls: list[tuple[str, dict[str, Any]]]
) -> tuple[list[Any], list[dict[str, Any]]]:
    command, args = _command()
    environ = dict(os.environ, WA_MCP_CONFIG=str(cfg))
    params = StdioServerParameters(command=command, args=args, env=environ)
    results: list[dict[str, Any]] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        for name, arguments in calls:
            res = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=90))
            results.append(json.loads("".join(getattr(c, "text", "") for c in res.content)))
    return tools, results


def test_import_tool_over_stdio(env: dict[str, Any]) -> None:
    inbox: Path = env["inbox"]
    good = inbox / "chat.zip"
    good.write_bytes(b"PK")
    outside = env["root"] / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    in_store = env["root"] / "store" / "notes.txt"
    in_store.write_text("x", encoding="utf-8")
    FakeBridge.requests.clear()

    tools, res = asyncio.run(
        _session(
            env["cfg"],
            [
                ("import_chat_export", {"path": str(good), "chat": "Alice", "me": "Bob", "dry_run": True}),
                (
                    "import_chat_export",
                    {"path": str(good), "chat": "+1 555 0100 002", "date_order": "dmy", "timezone": "UTC"},
                ),
                ("import_chat_export", {"path": str(outside), "chat": "Alice"}),
                ("import_chat_export", {"path": str(in_store), "chat": "Alice"}),
                ("import_chat_export", {"path": str(inbox / "none.txt"), "chat": "Alice"}),
                ("import_chat_export", {"path": str(good), "chat": "Alice", "me": "Nobody"}),
                ("import_chat_export", {"path": str(good), "chat": synth.GROUP}),
                ("import_chat_export", {"path": str(good), "chat": "Zed"}),
            ],
        )
    )
    tool = next(t for t in tools if t.name == "import_chat_export")
    hints = tool.annotations
    assert hints is not None and hints.readOnlyHint is False and hints.destructiveHint is False
    assert hints.idempotentHint is True and hints.openWorldHint is False
    assert "dry_run" in tool.inputSchema["properties"]

    dry, plain, outside_res, store_res, missing, wrong_me, no_chat, unknown = res
    assert dry["ok"] is True and dry["dry_run"] is True and dry["messages_added"] == 5 and "ok" in dry
    assert "http_status" not in dry and common.UNTRUSTED in dry["note"]
    assert plain["ok"] is True and plain["dry_run"] is False
    first, second = FakeBridge.requests[0]["body"], FakeBridge.requests[1]["body"]
    assert first == {"path": str(good.resolve()), "chat": synth.ALICE, "me": "Bob", "dry_run": True}
    assert second == {
        "path": str(good.resolve()),
        "chat": synth.ALICE_LID if second["chat"] == synth.ALICE_LID else synth.ALICE,
        "date_order": "dmy",
        "timezone": "UTC",
    }
    assert "outside the folders" in outside_res["error"] and outside_res["ok"] is False
    assert "never imported" in store_res["error"]
    assert "no such file" in missing["error"]
    assert wrong_me["ok"] is False and wrong_me["senders"] == ["Alice", "Bob"]
    assert no_chat["candidates"] == [{"jid": synth.GROUP, "title": "Book club"}]
    assert unknown["ok"] is False and "matches" in unknown["error"]
    assert len(FakeBridge.requests) == 4  # refused files and unknown chats never reach the bridge


def test_importable_file_rules(env: dict[str, Any]) -> None:
    common.setup(config.load(env["cfg"]))
    root: Path = env["root"]
    for folder, name in (("downloads", "a.txt"), ("exports", "b.zip"), ("inbox", "c.txt")):
        path = root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        assert common.importable_file(str(path)).name == name
    other = root / "other" / "d.txt"
    other.parent.mkdir(exist_ok=True)
    other.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside the folders"):
        common.importable_file(str(other))
    with pytest.raises(ValueError, match="never imported"):
        common.importable_file(str(env["cfg"]))
    with pytest.raises(ValueError, match="no such file"):
        common.importable_file(str(root / "inbox" / "missing.txt"))
    link = root / "inbox" / "link.txt"
    try:
        link.symlink_to(other)
    except OSError:
        return  # no permission to create links here
    with pytest.raises(ValueError, match="outside the folders"):
        common.importable_file(str(link))
