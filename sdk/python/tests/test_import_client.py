"""Bridge.import_export: the request it sends and the answers it passes on (no network, mock transport)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wa_mcp import config
from wa_mcp.client import TIMEOUTS, Bridge, BridgeError

REPORT = {
    "ok": True,
    "dry_run": False,
    "chat": "15550100002@s.whatsapp.net",
    "messages_added": 5,
    "duplicates": 1,
    "skipped_lines": {"count": 0, "first": []},
}


def _bridge(tmp_path: Path, handler: httpx.MockTransport) -> Bridge:
    (tmp_path / "store").mkdir(exist_ok=True)
    cfg = config.load(tmp_path / "config.json")
    return Bridge(cfg=cfg, token="t" * 32, transport=handler)


def test_import_export_sends_only_what_was_given(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=REPORT)

    with _bridge(tmp_path, httpx.MockTransport(handler)) as b:
        res = b.import_export("/data/exports/chat.zip", "15550100002")
        assert res["messages_added"] == 5 and res["http_status"] == 200
        assert seen[-1].method == "POST" and seen[-1].url.path == "/api/v1/import"
        assert json.loads(seen[-1].content) == {"path": "/data/exports/chat.zip", "chat": "15550100002"}

        b.import_export(
            "/data/exports/chat.txt",
            "Alice",
            me="Bob",
            date_order="mdy",
            dry_run=True,
            timezone="Europe/Berlin",
            names={"Carol": "15550100003"},
        )
        assert json.loads(seen[-1].content) == {
            "path": "/data/exports/chat.txt",
            "chat": "Alice",
            "me": "Bob",
            "date_order": "mdy",
            "timezone": "Europe/Berlin",
            "dry_run": True,
            "names": {"Carol": "15550100003"},
        }


def test_import_has_a_long_timeout() -> None:
    assert TIMEOUTS["import"] >= 300


def test_import_error_keeps_candidates_and_senders(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["chat"] == "Book club":
            return httpx.Response(
                409,
                json={
                    "ok": False,
                    "error": "2 chats are titled",
                    "candidates": [
                        {"jid": "1@g.us", "title": "Book club"},
                        {"jid": "2@g.us", "title": "Book club"},
                    ],
                },
            )
        return httpx.Response(
            400, json={"ok": False, "error": "cannot tell which sender is you", "senders": ["A", "B"]}
        )

    with _bridge(tmp_path, httpx.MockTransport(handler)) as b:
        with pytest.raises(BridgeError) as err:
            b.import_export("/x/a.txt", "Book club")
        assert err.value.status == 409 and len(err.value.data["candidates"]) == 2
        with pytest.raises(BridgeError) as err:
            b.import_export("/x/a.txt", "Alice")
        assert err.value.data["senders"] == ["A", "B"]


def test_plain_bridge_errors_have_no_extra_data(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _bridge(tmp_path, httpx.MockTransport(handler)) as b, pytest.raises(BridgeError) as err:
        b.status()
    assert err.value.data == {}


def test_import_defaults_are_in_the_config(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "config.json")
    assert cfg.section("import") == {"allowed_dirs": [], "max_mb": 1024, "omitted_phrases": []}
