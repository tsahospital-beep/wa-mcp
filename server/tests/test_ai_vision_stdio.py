"""Image text, image descriptions and the get_my_day summary through the real MCP stdio server.

The AI provider is a small HTTP server on 127.0.0.1 that speaks the OpenAI chat completions format.
OCR runs a fake tesseract program that is put first on PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from vision_data import PNG, fake_tesseract

from wa_mcp_server.common import UNTRUSTED

KEY = "stdio-test-key-42"
T0_DAY = datetime.fromtimestamp(synth.T0).date().isoformat()
NOT_CONFIGURED = "no AI provider configured; your assistant can summarize this"


class FakeAI(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        FakeAI.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        user = body["messages"][-1]["content"]
        text = "A screenshot of an invoice." if isinstance(user, list) else "Two things need you today."
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: Any) -> None:
        return


def _store(root: Path, config_extra: dict[str, Any], images: list[str]) -> Path:
    cfg = synth.make_config(root, **config_extra)
    synth.build_wa_db(root / "store" / "wa.db")
    synth.build_session_db(root / "store" / "session.db")
    conn = sqlite3.connect(root / "store" / "wa.db")
    for i, msg_id in enumerate(images):
        path = root / "downloads" / f"{msg_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        conn.execute(
            "INSERT INTO chat_messages (conv, msg_id, author, sent_at, body, kind, mime, local_path) "
            "VALUES (?, ?, ?, ?, '', 'image', 'image/png', ?)",
            (synth.ALICE, msg_id, synth.ALICE, synth.T0 + 1000 + i * 10, str(path)),
        )
    conn.commit()
    conn.close()
    return cfg


async def _session(
    cfg: Path, calls: list[tuple[str, dict[str, Any]]], path_first: Path | None = None
) -> list[dict[str, Any]]:
    scripts = Path(sys.executable).parent
    exe = shutil.which("wa-mcp", path=str(scripts))
    command, args = (exe, []) if exe else (sys.executable, ["-m", "wa_mcp_server"])
    environ = dict(os.environ, WA_MCP_CONFIG=str(cfg), WA_MCP_AI_TEST_KEY=KEY)
    environ.pop("WA_MCP_SENDING_ENABLED", None)
    if path_first is not None:
        environ["PATH"] = f"{path_first}{os.pathsep}{environ.get('PATH', '')}"
    params = StdioServerParameters(command=command, args=[*args, "--toolsets", "all"], env=environ)
    results: list[dict[str, Any]] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for name, arguments in calls:
            res = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=90))
            data = json.loads("".join(getattr(c, "text", "") for c in res.content))
            data["_raw"] = "".join(getattr(c, "text", "") for c in res.content)
            results.append(data)
    return results


@pytest.fixture(scope="module")
def ai_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture(scope="module")
def with_provider(tmp_path_factory: pytest.TempPathFactory, ai_server: str) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("ai-on")
    folder = root / "bin"
    fake_tesseract(folder, out="Invoice 4711\nDue Friday\n")
    cfg = _store(
        root,
        {
            "ai": {
                "provider": "openai-compatible",
                "base_url": ai_server,
                "text_model": "text-m",
                "vision_model": "vision-m",
                "api_key_env": "WA_MCP_AI_TEST_KEY",
                "max_calls_per_day": 3,
            },
            "vision": {"ocr": "tesseract", "languages": ["eng"]},
        },
        ["IMG1", "IMG2"],
    )
    FakeAI.requests.clear()
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_bridge_status", {}),
        ("read_images", {"chat": "Alice", "describe": True}),
        ("list_messages", {"chat": "Alice", "query": "4711"}),
        ("get_media_text", {"chat": "Alice", "message_id": "IMG1"}),
        ("read_images", {"chat": "Alice", "describe": True}),
        ("get_my_day", {"date": T0_DAY, "summary": True}),
        ("get_my_day", {"date": T0_DAY}),
        ("get_my_day", {"date": T0_DAY, "summary": True}),
        ("read_images", {"chat": "Alice", "describe": True, "force": True}),
    ]
    results = asyncio.run(_session(cfg, calls, path_first=folder))
    by: dict[str, list[dict[str, Any]]] = {}
    for (name, _), res in zip(calls, results, strict=True):
        by.setdefault(name, []).append(res)
    return {"by": by, "results": results, "requests": list(FakeAI.requests)}


def test_status_reports_ocr_and_provider(with_provider: dict[str, Any]) -> None:
    status = with_provider["by"]["get_bridge_status"][0]
    assert status["ai_provider"] == "openai-compatible" and status["ocr_engine"] == "tesseract"
    assert status["ocr_ready"] is True


def test_read_images_reads_and_describes(with_provider: dict[str, Any]) -> None:
    first = with_provider["by"]["read_images"][0]
    assert first["ok"] is True and first["complete"] is True and first["candidates"] == 2
    assert {r["msg_id"] for r in first["read"]} == {"IMG1", "IMG2"}
    item = first["read"][0]
    assert item["text"] == "Invoice 4711\nDue Friday" and item["description"] == "A screenshot of an invoice."
    assert first["note"] == UNTRUSTED


def test_image_text_is_searchable_and_shown(with_provider: dict[str, Any]) -> None:
    found = with_provider["by"]["list_messages"][0]["messages"]
    assert {m["id"] for m in found} == {"IMG1", "IMG2"}
    assert found[0]["image_text"] == "Invoice 4711\nDue Friday"
    assert found[0]["image_description"] == "A screenshot of an invoice."
    media = with_provider["by"]["get_media_text"][0]
    assert media["ok"] and media["kind"] == "image"
    assert {(i["source"], i["text"]) for i in media["items"]} == {
        ("image", "Invoice 4711\nDue Friday"),
        ("image_description", "A screenshot of an invoice."),
    }


def test_second_run_skips_finished_images(with_provider: dict[str, Any]) -> None:
    second = with_provider["by"]["read_images"][1]
    assert second["ok"] and second["read"] == [] and second["already_done"] == 2


def test_my_day_summary_and_the_default(with_provider: dict[str, Any]) -> None:
    day = with_provider["by"]["get_my_day"]
    assert day[0]["ok"] and day[0]["ai_summary"] == "Two things need you today."
    assert day[0]["ai_provider"] == "openai-compatible" and isinstance(day[0]["people"], list)
    assert "ai_summary" not in day[1] and "ai_note" not in day[1] and day[1]["summary"].startswith(T0_DAY)
    assert [g["chat"] for g in day[1]["people"]] == [g["chat"] for g in day[0]["people"]]


def test_daily_limit_stops_ai_calls(with_provider: dict[str, Any]) -> None:
    limited = with_provider["by"]["get_my_day"][2]
    assert limited["ok"] is True and limited["ai_summary"] is None
    assert "ai.max_calls_per_day is 3" in limited["ai_error"] and limited["people"]
    forced = with_provider["by"]["read_images"][2]
    assert forced["ok"] is True and forced["read"] == [] and "max_calls_per_day" in forced["stopped"]
    assert len(with_provider["requests"]) == 3  # two descriptions and one summary reached the provider


def test_provider_requests_are_framed_and_authenticated(with_provider: dict[str, Any]) -> None:
    requests = with_provider["requests"]
    assert all(r["path"] == "/v1/chat/completions" and r["auth"] == f"Bearer {KEY}" for r in requests)
    vision_requests = [r for r in requests if isinstance(r["body"]["messages"][-1]["content"], list)]
    summary = next(r for r in requests if isinstance(r["body"]["messages"][-1]["content"], str))
    assert len(vision_requests) == 2 and vision_requests[0]["body"]["model"] == "vision-m"
    url = vision_requests[0]["body"]["messages"][-1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert summary["body"]["model"] == "text-m"
    system, user = (m["content"] for m in summary["body"]["messages"])
    assert "never instructions" in system and "<untrusted_data>" in user and "</untrusted_data>" in user
    assert T0_DAY in user
    for raw in with_provider["results"]:
        assert KEY not in raw["_raw"]


def test_without_a_provider_or_ocr(tmp_path: Path) -> None:
    cfg = _store(
        tmp_path,
        {"vision": {"ocr": "tesseract", "tesseract_binary": str(tmp_path / "missing" / "tesseract")}},
        ["IMG1"],
    )
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_bridge_status", {}),
        ("read_images", {"chat": "Alice"}),
        ("read_images", {"chat": "Alice", "describe": True}),
        ("get_my_day", {"date": T0_DAY, "summary": True}),
        ("get_media_text", {"chat": "Alice", "message_id": "IMG1"}),
    ]
    status, plain, described, day, media = asyncio.run(_session(cfg, calls))
    assert status["ai_provider"] == "none" and status["ocr_ready"] is False
    assert "tesseract was not found" in status["ocr_hint"]
    assert plain["ok"] is False and "tesseract was not found" in plain["error"]
    assert described["ok"] is False and "ai.provider" in described["error"]
    assert (
        day["ok"] is True and day["ai_note"] == NOT_CONFIGURED and "ai_summary" not in day and day["people"]
    )
    assert media["items"] == [] and "run read_images" in media["hint"]

    off = _store(tmp_path / "off", {"vision": {"ocr": "none"}}, ["IMG1"])
    (res,) = asyncio.run(_session(off, [("read_images", {"chat": "Alice"})]))
    assert res["ok"] is False and "vision.ocr" in res["error"]
