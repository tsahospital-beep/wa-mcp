"""Every MCP tool through the real stdio client against a fake bridge and a synthetic store."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
import wave
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
from mcp.types import ElicitResult

from wa_mcp_server.common import UNTRUSTED

TOKEN = "test-token-0123456789"
THUMBS = "\U0001f44d"
EXPECTED_TOOLS = {
    "search_contacts", "list_chats", "get_chat", "list_messages", "get_message_context", "get_last_interaction",
    "request_chat_history", "sync_chat_history", "sync_all_history", "download_media", "download_chat_media",
    "export_chat", "export_period", "get_media_text", "list_stickers", "transcribe_chat_audio",
    "extract_file_text", "fetch_shared_docs", "send_message", "send_file", "send_voice_note", "send_sticker",
    "mark_as_read", "list_calls", "schedule_message", "list_scheduled", "cancel_scheduled", "bridge_status",
}  # fmt: skip
INSIGHT_TOOLS = {
    "my_day", "chat_analytics", "chat_analytics_compare", "detect_tasks", "tasks_overview", "task_detail",
    "which_task", "refresh_tasks", "project_overview", "whats_new", "find_mentions",
}  # fmt: skip
T0_DAY = datetime.fromtimestamp(synth.T0).date().isoformat()


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(
            b"".join(int(6000 * math.sin(i / 5)).to_bytes(2, "little", signed=True) for i in range(16000))
        )
    return path


class FakeBridge(BaseHTTPRequestHandler):
    root: Path
    requests: ClassVar[list[tuple[str, str, Any]]] = []

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
        FakeBridge.requests.append((method, path, body))
        if path == "/status":
            self._reply(
                200,
                {
                    "ok": True,
                    "connected": True,
                    "logged_in": True,
                    "account": "+1 *** 0101",
                    "version": "t",
                    "app_state": {"regular_high": "recovering"},
                },
            )
        elif path == "/agent/reply":
            self._reply(200, {"ok": True})
        elif path == "/send/preview":
            text = body["text"]
            parts = [text] if len(text) <= 4096 else ["(1/2) " + text[:4000], "(2/2) " + text[4000:]]
            self._reply(200, {"ok": True, "length": len(text), "parts": parts})
        elif path == "/send":
            ids = ["SENT1", "SENT2"] if len(body.get("text") or "") > 4096 else []
            self._reply(200, {"ok": True, "id": "SENT1", "ids": ids, "sent_at": synth.T0, "waited_ms": 0})
        elif path == "/read":
            self._reply(200, {"ok": True, "marked": len(body["ids"])})
        elif path == "/history":
            conn = sqlite3.connect(self.root / "store" / "wa.db")
            conn.execute(
                "INSERT OR IGNORE INTO chat_messages (conv, msg_id, author, sent_at, body) VALUES (?, ?, ?, ?, ?)",
                (body["chat"], "OLD-" + body["chat"][:6], body["chat"], synth.T0 - 400_000, "old message"),
            )
            conn.commit()
            conn.close()
            self._reply(
                200, {"ok": True, "requested": body.get("count", 50), "anchor_id": "x", "oldest_known": 1}
            )
        elif path == "/media/download":
            out = self.root / "downloads" / f"{body['id']}.bin"
            out.parent.mkdir(parents=True, exist_ok=True)
            if body["id"] == "A3":
                out = _wav(out.with_suffix(".wav"))
            elif body["id"] == "A4":
                out = out.with_suffix(".txt")
                out.write_text("Plan: meet at the library", encoding="utf-8")
            else:
                out.write_bytes(b"RIFF0000WEBP")
            self._reply(200, {"ok": True, "path": str(out), "kind": "document"})
        elif path == "/schedule" and method == "POST":
            self._reply(201, {"ok": True, "id": 9, "next_run": synth.T0 + 5000, "scheduled": {}})
        elif path.startswith("/schedule/") and method == "DELETE":
            self._reply(200, {"ok": True, "cancelled": True, "id": int(path.rsplit("/", 1)[1])})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("mcp")
    FakeBridge.root = root
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stt = root / "stt.py"
    stt.write_text("print('transcribed words')", encoding="utf-8")
    media = root / "media"
    media.mkdir()
    cfg = synth.make_config(
        root,
        port=server.server_address[1],
        sending={
            "enabled": True,
            "require_confirm": True,
            "allow_confirm_flag": True,
            "allowed_media_dirs": [str(media)],
        },
        transcription={"backend": "command", "command": {"argv": [sys.executable, str(stt), "{file}"]}},
    )
    synth.build_wa_db(root / "store" / "wa.db")
    synth.build_session_db(root / "store" / "session.db")
    conn = sqlite3.connect(root / "store" / "wa.db")
    conn.executemany(
        "INSERT INTO reactions (conv, msg_id, reactor, emoji, reacted_at) VALUES (?, 'A2', ?, ?, ?)",
        [
            (synth.ALICE, synth.ALICE, THUMBS, synth.T0 + 210),
            (synth.ALICE, synth.CAROL_LID, THUMBS, synth.T0 + 220),
        ],
    )
    streak = "15550100077@s.whatsapp.net"
    conn.executemany(
        "INSERT INTO chat_messages (conv, msg_id, author, sent_at, outgoing) VALUES (?, ?, ?, ?, ?)",
        [(streak, f"streak{i}", streak, i + 1, int(i == 0)) for i in range(202)],
    )
    conn.commit()
    conn.close()
    yield {"root": root, "cfg": cfg, "media": media}
    server.shutdown()


def _free_port() -> int:
    """A port nothing listens on, so a test that should not reach the bridge cannot reach a real one."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _command() -> tuple[str, list[str]]:
    scripts = Path(sys.executable).parent
    exe = shutil.which("wa-mcp", path=str(scripts))
    return (exe, []) if exe else (sys.executable, ["-m", "wa_mcp_server"])


async def _session(
    cfg: Path,
    calls: list[tuple[str, dict[str, Any]]],
    elicit: Any = None,
    tools_out: list[Any] | None = None,
) -> tuple[set[str], list[dict[str, Any]]]:
    command, args = _command()
    environ = dict(os.environ, WA_MCP_CONFIG=str(cfg))
    environ.pop("WA_MCP_SENDING_ENABLED", None)
    params = StdioServerParameters(command=command, args=args, env=environ)
    results: list[dict[str, Any]] = []
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, elicitation_callback=elicit) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools
        if tools_out is not None:
            tools_out.extend(tools)
        names = {t.name for t in tools}
        for name, arguments in calls:
            res = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=90))
            text = "".join(getattr(c, "text", "") for c in res.content)
            data = json.loads(text)
            data["_raw"] = text
            results.append(data)
    return names, results


@pytest.fixture(scope="module")
def run(env: dict[str, Any]) -> dict[str, Any]:
    sticker = env["media"] / "wave.webp"
    sticker.write_bytes(b"RIFF0000WEBPVP8 ")
    photo = env["media"] / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    voice = _wav(env["media"] / "hello.wav")
    outside = _wav(env["root"] / "outside.wav")
    in_store = env["root"] / "store" / "inside.ogg"
    in_store.write_bytes(b"OggS")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("bridge_status", {}),
        ("search_contacts", {"query": "ally"}),
        ("list_chats", {"offset": -12}),
        ("get_chat", {"chat": "Alice"}),
        ("get_chat", {"chat": synth.ALICE_LID.replace("@lid", "@s.whatsapp.net")}),
        ("list_messages", {"chat": "Alice", "limit": 10, "offset": -12}),
        ("list_messages", {"query": "éCLAIR"}),
        ("get_message_context", {"chat": synth.ALICE, "message_id": "A2", "before": 1, "after": 1}),
        ("get_last_interaction", {"chat": "+15550100002"}),
        ("get_last_interaction", {"chat": "+15550100077"}),
        ("request_chat_history", {"chat": "Bob", "count": 20}),
        ("sync_chat_history", {"chat": "Bob", "back_to": "2025-12-26", "max_pages": 3, "wait_seconds": 2}),
        (
            "sync_all_history",
            {"active_since": "2020-01-01", "back_to": "2020-01-01", "max_chats": 2, "wait_seconds": 1},
        ),
        ("download_media", {"chat": "Book club", "message_id": "G2"}),
        ("download_chat_media", {"chat": "Alice", "kinds": ["document"]}),
        ("extract_file_text", {"chat": "Alice", "message_id": "A4"}),
        ("get_media_text", {"chat": "Alice", "message_id": "A4"}),
        ("transcribe_chat_audio", {"chat": "Alice", "full_archive": True, "force": True}),
        ("list_stickers", {}),
        ("fetch_shared_docs", {"chat": "Bob"}),
        ("export_chat", {"chat": "Alice"}),
        ("export_period", {"after": "2020-01-01"}),
        ("send_message", {"to": "Alice", "text": "See you at seven"}),
        ("send_message", {"to": "Alice", "text": "See you at seven", "reply_to": "A1", "confirm": True}),
        ("send_file", {"to": "+1 555 0100 003", "path": str(photo), "caption": "the cover", "confirm": True}),
        ("send_voice_note", {"to": "+1 555 0100 003", "path": str(voice), "confirm": True}),
        ("send_sticker", {"to": synth.GROUP, "path": str(sticker), "mentions": None, "confirm": True}),
        ("send_message", {"to": "Ally", "text": "push names are not targets", "confirm": True}),
        ("send_voice_note", {"to": synth.BOB, "path": str(outside), "confirm": True}),
        ("send_file", {"to": synth.BOB, "path": str(in_store), "confirm": True}),
        ("list_messages", {"chat": synth.GROUP}),
        ("list_messages", {"kinds": ["location", "contact", "poll", "unsupported", "ptv"]}),
        ("list_messages", {"kinds": ["hologram"]}),
        ("check_links", {"chat": "Bob"}),
        ("send_message", {"to": synth.ALICE, "text": "x" * 12001, "confirm": True}),
        ("mark_as_read", {"chat": "Alice", "message_ids": ["A1"]}),
        ("list_calls", {"limit": 1, "offset": 1}),
        (
            "schedule_message",
            {"to": synth.GROUP, "at": "2030-01-01T18:00:00Z", "text": "Reading", "confirm": True},
        ),
        ("list_scheduled", {"status": "pending"}),
        ("cancel_scheduled", {"scheduled_id": 1}),
        ("refresh_tasks", {"days": 3650}),
        ("my_day", {"date": T0_DAY}),
        ("chat_analytics", {"chat": "Alice", "detail": "brief"}),
        ("chat_analytics_compare", {"chats": ["Alice", "Bob"]}),
        ("detect_tasks", {"chat": "Book club"}),
        ("tasks_overview", {"open_only": False}),
        ("task_detail", {"query": "book"}),
        ("which_task", {"message_id": "A1"}),
        ("project_overview", {"project": "Reading"}),
        ("whats_new", {"hours": 24 * 3650}),
        ("find_mentions", {"days": 3650, "words": ["library"]}),
    ]
    names, results = asyncio.run(_session(env["cfg"], calls))
    by: dict[str, list[dict[str, Any]]] = {}
    for (name, _), res in zip(calls, results, strict=True):
        by.setdefault(name, []).append(res)
    return {"names": names, "by": by, "results": results}


def test_all_tools_listed(run: dict[str, Any]) -> None:
    assert run["names"] >= EXPECTED_TOOLS


def test_no_secret_in_any_output(run: dict[str, Any]) -> None:
    for res in run["results"]:
        assert TOKEN not in res["_raw"]
        assert "media_key" not in res["_raw"]


def test_read_tools(run: dict[str, Any]) -> None:
    by = run["by"]
    assert by["bridge_status"][0]["bridge"]["connected"] is True
    assert by["bridge_status"][0]["app_state"] == {"regular_high": "recovering"}
    assert by["bridge_status"][0]["transcription_ready"] is True
    assert by["search_contacts"][0]["contacts"][0]["jids"] == [synth.ALICE, synth.ALICE_LID]
    assert by["list_chats"][0]["offset"] == 0
    assert by["list_messages"][0]["next_offset"] == by["list_messages"][0]["count"]
    assert by["get_last_interaction"][1]["last_from_me"]["id"] == "streak0"
    chats = by["list_chats"][0]["chats"]
    assert chats[0]["jids"] == [synth.ALICE, synth.ALICE_LID] and chats[0]["name"] == "Alice Example"
    missing = by["get_chat"][1]
    assert missing["ok"] and missing["found"] is False and "chat" not in missing
    assert by["get_chat"][0]["found"]
    assert by["get_chat"][0]["chat"]["phone"] == "+15550100002"
    msgs = by["list_messages"][0]["messages"]
    assert {m["id"] for m in msgs} >= {"L1", "A1", "A3"}
    a2 = next(m for m in msgs if m["id"] == "A2")
    assert a2["reply_to"]["author"] == "Alice Example" and a2["from"] == "me"
    assert "untrusted" in by["list_messages"][0]["note"]
    assert [m["id"] for m in by["list_messages"][1]["messages"]] == ["A3"]
    ctx = by["get_message_context"][0]
    assert ctx["before"][0]["id"] == "A1" and ctx["after"][0]["id"] == "A3"
    assert by["get_last_interaction"][0]["last"]["id"] == "L1"
    assert by["get_last_interaction"][0]["last_from_me"]["id"] == "A2"
    assert by["list_calls"][0]["calls"][0]["id"] == "C1"
    assert by["list_stickers"][0]["stickers"][0]["id"] == "G2"


def test_history_tools(run: dict[str, Any]) -> None:
    by = run["by"]
    assert by["request_chat_history"][0]["ok"] is True
    sync = by["sync_chat_history"][0]
    assert sync["ok"] and sync["reached"] and sync["pages"] <= 1
    allsync = by["sync_all_history"][0]
    assert allsync["ok"] and allsync["chats"]
    assert allsync["chats"][0]["state"] in ("reached", "exhausted", "pending")


def test_media_and_text_tools(run: dict[str, Any], env: dict[str, Any]) -> None:
    by = run["by"]
    assert by["download_media"][0]["path"].endswith("G2.bin")
    assert by["download_chat_media"][0]["saved"][0]["msg_id"] == "A4"
    assert "library" in by["extract_file_text"][0]["text"]
    assert by["get_media_text"][0]["items"][0]["source"] == "document"
    tr = by["transcribe_chat_audio"][0]
    assert tr["ok"] and not tr["failed"], tr
    assert [item["text"] for item in tr["transcribed"]] == ["transcribed words"]
    docs = by["fetch_shared_docs"][0]
    assert docs["ok"] and docs["fetched"] == [] and docs["failed"] == [] and docs["note"] == UNTRUSTED
    export = Path(by["export_chat"][0]["path"])
    text = export.read_text(encoding="utf-8")
    assert "Transcript" in text and "transcribed words" in text and "Alice Example" in text
    assert by["export_period"][0]["chats"] >= 3


def test_send_tools(run: dict[str, Any], env: dict[str, Any]) -> None:
    by = run["by"]
    refused, sent = by["send_message"][:2]
    assert refused["ok"] is False and "confirm=true" in refused["error"]
    assert sent["ok"] and sent["id"] == "SENT1" and sent["to"] == synth.ALICE
    assert sent["to_name"] == "Alice Example"
    sends = [b for m, p, b in FakeBridge.requests if p == "/send"]
    assert sends[0]["reply_to"] == {"id": "A1"} and sends[0]["text"] == "See you at seven"
    assert sends[1]["media_path"].endswith("photo.jpg") and sends[1]["text"] == "the cover"
    assert by["send_file"][0]["to"] == synth.BOB and by["send_file"][0]["to_name"] == "~Bobby"
    if shutil.which("ffmpeg"):
        assert by["send_voice_note"][0]["ok"] and sends[2]["as_voice"] is True
        converted = Path(sends[2]["media_path"])
        assert converted.parent.parent == env["root"] / "exports" / "outgoing"
        assert not converted.exists() and not converted.parent.exists()
    assert by["send_sticker"][0]["ok"] and sends[-1]["sticker_path"].endswith("wave.webp")
    assert by["mark_as_read"][0]["marked"] == 1
    assert by["schedule_message"][0]["id"] == 9
    assert by["list_scheduled"][0]["scheduled"][0]["text"] == "Reading tonight"
    assert by["cancel_scheduled"][0]["cancelled"] is True


def test_insight_tools(run: dict[str, Any]) -> None:
    by = run["by"]
    assert run["names"] >= INSIGHT_TOOLS
    for name in INSIGHT_TOOLS:
        assert by[name][0]["ok"] is True, (name, by[name][0])
    assert by["refresh_tasks"][0]["chats"] >= 3
    day = by["my_day"][0]
    assert day["date"] == T0_DAY and day["summary"].startswith(T0_DAY) and isinstance(day["people"], list)
    assert UNTRUSTED in day["note"]
    ana = by["chat_analytics"][0]
    assert ana["meta"]["detail"] == "brief" and ana["sections"]["volume"]["n"] > 0
    assert ana["summary"].startswith("Chat with Alice Example")
    assert len(by["chat_analytics_compare"][0]["rows"]) == 2
    det = by["detect_tasks"][0]
    assert det["is_group"] is True and {d["category"] for d in det["detections"]} <= {
        "CHAT",
        "TEAM",
        "PRECURSOR",
    }
    assert by["tasks_overview"][0]["count"] == len(by["tasks_overview"][0]["tasks"])
    assert by["task_detail"][0]["found"] is False
    assert by["which_task"][0]["found"] is False
    assert by["project_overview"][0]["configured"] is False
    assert isinstance(by["whats_new"][0]["steps"], list)
    found = by["find_mentions"][0]
    assert found["count"] >= 1 and found["mentions"][0]["message_id"] == "A4"
    carries_text = INSIGHT_TOOLS - {"refresh_tasks"}
    missing = sorted(n for n in carries_text if UNTRUSTED not in by[n][0].get("note", ""))
    assert not missing, missing


def test_reactions_shown_read_only(run: dict[str, Any]) -> None:
    by = run["by"]
    a2 = next(m for m in by["list_messages"][0]["messages"] if m["id"] == "A2")
    assert a2["reactions"] == {
        "counts": {THUMBS: 2},
        "by": [{"name": "Alice Example", "emoji": THUMBS}, {"name": "~Carol", "emoji": THUMBS}],
    }
    assert by["get_message_context"][0]["message"]["reactions"]["counts"] == {THUMBS: 2}
    assert "reactions" not in next(m for m in by["list_messages"][0]["messages"] if m["id"] == "A1")
    assert "reactions" in UNTRUSTED


def test_kinds_and_check_links(run: dict[str, Any]) -> None:
    by = run["by"]
    assert by["list_messages"][3]["ok"] is True and by["list_messages"][3]["count"] == 0
    assert by["list_messages"][4]["ok"] is False and "hologram" in by["list_messages"][4]["error"]
    links = by["check_links"][0]
    assert links["ok"] is True and links["count"] == 0 and links["note"] == UNTRUSTED
    too_long = by["send_message"][3]
    assert too_long["ok"] is False and "12000" in too_long["error"]


def test_send_target_and_folder_rules(run: dict[str, Any]) -> None:
    by = run["by"]
    push = by["send_message"][2]
    assert push["ok"] is False and "no contact named exactly" in push["error"]
    outside = by["send_voice_note"][1]
    assert outside["ok"] is False and "outside the folders allowed" in outside["error"]
    in_store = by["send_file"][1]
    assert in_store["ok"] is False and "store_dir" in in_store["error"]
    sent_paths = [b.get("media_path") or "" for m, p, b in FakeBridge.requests if p == "/send"]
    assert not any("outside" in x or "inside.ogg" in x for x in sent_paths)


def test_untrusted_notes_and_push_names(run: dict[str, Any]) -> None:
    by = run["by"]
    for name in (
        "search_contacts",
        "list_chats",
        "get_chat",
        "list_calls",
        "list_scheduled",
        "list_stickers",
        "download_media",
        "download_chat_media",
    ):
        assert by[name][0]["note"] == UNTRUSTED, name
    assert "file names" in UNTRUSTED
    group = by["list_messages"][2]["messages"]
    assert next(m for m in group if m["id"] == "G1")["from"] == "~Carol"
    assert by["search_contacts"][0]["contacts"][0]["name_source"] == "contact"


def test_tool_annotations(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"store_dir":"private","api":{"listen":"127.0.0.1:0"}}', encoding="utf-8")
    tools: list[Any] = []
    asyncio.run(_session(cfg, [], tools_out=tools))
    ann = {t.name: t.annotations for t in tools}
    for name in (
        "send_message",
        "send_file",
        "send_voice_note",
        "send_sticker",
        "schedule_message",
        "mark_as_read",
    ):
        assert ann[name] is not None and ann[name].destructiveHint is True, name
    for name in (
        "list_messages",
        "list_chats",
        "search_contacts",
        "get_media_text",
        "list_calls",
        "bridge_status",
    ):
        assert ann[name] is not None and ann[name].readOnlyHint is True, name
    # Tools that may refresh the local task cache (insight.db) are honest about it.
    writes = {"refresh_tasks", "my_day", "chat_analytics", "tasks_overview", "task_detail", "which_task"}
    writes |= {"project_overview", "whats_new"}
    for name in INSIGHT_TOOLS - writes:
        assert ann[name] is not None and ann[name].readOnlyHint is True, name
    for name in writes:
        a = ann[name]
        assert a is not None and a.readOnlyHint is False and a.destructiveHint is False, name
    assert ann["check_links"] is not None and ann["check_links"].openWorldHint is True


def test_agent_tools_over_stdio(env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from wa_mcp import accounts, config
    from wa_mcp.agent import worker
    from wa_mcp.client import Bridge

    data = json.loads(env["cfg"].read_text())
    data.update(
        agent={
            "enabled": True,
            "allow_chats": ["Alice"],
            "mode": "approve",
            "provider": "agents.custom_provider:Assistant",
        }
    )
    data["sending"]["require_confirm"] = False
    data["store_dir"] = str(env["root"] / "store")
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(data))
    accounts.connect(config.load(path))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(synth.REPO / "examples"), os.getenv("PYTHONPATH", "")])
    )
    draft = asyncio.run(
        worker.handle(
            Mock(spec=Bridge), {"id": "A1", "chat": synth.ALICE, "kind": "message", "at": time.time()}
        )
    )
    assert draft["status"] == "drafted"
    args = {"draft_id": draft["draft_id"]}
    asked = []

    async def elicit(context: Any, params: Any) -> Any:
        asked.append(params.message)
        return ElicitResult(action="accept", content={"approve": True})

    calls = [
        ("agent_status", {}),
        ("agent_test", {}),
        ("agent_drafts", {}),
        ("agent_pause", {}),
        ("agent_resume", {}),
        ("agent_approve_draft", args),
        ("agent_approve_draft", args),
    ]
    _, results = asyncio.run(_session(path, calls, elicit=elicit))
    assert results[0]["mode"] == "approve" and results[1]["preview"] == "Thanks for your message."
    assert results[2]["drafts"][0]["id"] == draft["draft_id"]
    assert results[3]["paused"] and not results[4]["paused"]
    assert results[5]["status"] == "sent" and not results[6]["ok"]
    assert len(asked) == 1 and "Thanks for your message." in asked[0] and synth.ALICE in asked[0]


def test_send_asks_the_user_through_elicitation(env: dict[str, Any]) -> None:
    asked: list[str] = []
    answers = iter(["accept", "decline"])

    async def elicit(context: Any, params: Any) -> Any:
        asked.append(params.message)
        action = next(answers)
        return ElicitResult(action=action, content={"approve": True} if action == "accept" else None)

    long_text = "Approved in the client. " + "word " * 900 + "THE END"
    calls = [
        (
            "send_message",
            {
                "to": "Alice Example",
                "text": long_text,
                "reply_to": "A1",
                "mentions": [synth.BOB, synth.CAROL_LID],
            },
        ),
        ("send_message", {"to": "Alice Example", "text": "Declined in the client", "confirm": True}),
    ]
    _, results = asyncio.run(_session(env["cfg"], calls, elicit=elicit))
    assert results[0]["ok"] is True and results[0]["to_name"] == "Alice Example"
    assert results[1]["ok"] is False and "did not approve" in results[1]["error"]
    prompt = asked[0]
    assert f"To: Alice Example (address book, {synth.ALICE})" in prompt
    assert len(asked) == 2
    assert results[0]["ids"] == ["SENT1", "SENT2"]
    assert f"{len(long_text)} characters, 2 parts" in prompt
    assert "(1/2) " + long_text[:4000] in prompt and "(2/2) " + long_text[4000:] in prompt
    assert f"Bobby (push name, {synth.BOB})" in prompt and f"Carol (push name, {synth.CAROL_LID})" in prompt
    assert "Reply to Alice Example (address book" in prompt and "Hello from Alice" in prompt


def test_no_elicitation_needs_allow_confirm_flag(tmp_path: Path) -> None:
    cfg = synth.make_config(tmp_path, port=_free_port(), sending={"enabled": True, "require_confirm": True})
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    synth.build_session_db(tmp_path / "store" / "session.db")
    _, results = asyncio.run(
        _session(cfg, [("send_message", {"to": synth.ALICE, "text": "x", "confirm": True})])
    )
    assert results[0]["ok"] is False and "allow_confirm_flag" in results[0]["error"]


def test_bridge_status_reports_failed_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wa_mcp import config
    from wa_mcp_server import app

    (tmp_path / "broken_tools_mod.py").write_text(
        "def register(mcp):\n    raise RuntimeError('missing dependency')\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(app, "OPTIONAL_MODULES", ("broken_tools_mod",))
    cfg = synth.make_config(tmp_path, port=_free_port())
    mcp = app.create_app(config.load(cfg))
    out = asyncio.run(mcp.call_tool("bridge_status", {}))
    blocks = out[0] if isinstance(out, tuple) else out
    data = json.loads("".join(getattr(b, "text", "") for b in blocks))
    assert data["tool_modules_failed"] == {"broken_tools_mod": "RuntimeError: missing dependency"}


def test_sending_disabled_by_default(tmp_path: Path) -> None:
    cfg = synth.make_config(tmp_path, port=_free_port())
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    _, results = asyncio.run(
        _session(cfg, [("send_message", {"to": synth.ALICE, "text": "x", "confirm": True})])
    )
    assert results[0]["ok"] is False and "disabled" in results[0]["error"]
