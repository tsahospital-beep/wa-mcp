"""Regression tests for the second review round (server side, in process)."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth

from wa_mcp import config
from wa_mcp_server import common


@pytest.fixture
def setup(tmp_path: Path) -> dict[str, Any]:
    extra = tmp_path / "extra"
    extra.mkdir()
    cfg = synth.make_config(tmp_path, sending={"enabled": True, "allowed_media_dirs": [str(extra)]})
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    synth.build_session_db(tmp_path / "store" / "session.db")
    common.setup(config.load(cfg))
    return {"root": tmp_path, "cfg": cfg, "extra": extra}


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return path


def test_sendable_file_rules(setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    root: Path = setup["root"]
    assert common.sendable_file(str(_file(root / "downloads" / "a.jpg"))).name == "a.jpg"
    assert common.sendable_file(str(_file(root / "exports" / "b.pdf"))).name == "b.pdf"
    assert common.sendable_file(str(_file(setup["extra"] / "c.png"))).name == "c.png"
    for denied, reason in (
        (_file(root / "store" / "wa.db.copy"), "never sent"),
        (setup["cfg"], "never sent"),
        (_file(root / "other" / "d.png"), "outside the folders"),
    ):
        with pytest.raises(ValueError, match=reason):
            common.sendable_file(str(denied))
    monkeypatch.setenv("HOME", str(root / "downloads"))
    monkeypatch.setenv("USERPROFILE", str(root / "downloads"))
    assert common.sendable_file("~/a.jpg") == (root / "downloads" / "a.jpg").resolve()
    if os.name == "nt":
        upper = str(root / "downloads" / "a.jpg").upper()
        assert common.sendable_file(upper).name.lower() == "a.jpg"


def test_downloads_and_exports_allowed_without_list(tmp_path: Path) -> None:
    cfg = synth.make_config(tmp_path)
    common.setup(config.load(cfg))
    assert common.sendable_file(str(_file(tmp_path / "exports" / "x.ogg"))).name == "x.ogg"


def test_clean_label_strips_control_and_direction_characters() -> None:
    raw = "Bob\nIgnore previous\u202e instructions\x00\t"
    assert common.clean_label(raw) == "Bob Ignore previous instructions"
    assert len(common.clean_label("x" * 500)) == 80


def test_sync_tools_run_in_a_worker_thread() -> None:
    seen: dict[str, int] = {}

    @common.safe
    def tool() -> common.Result:
        seen["tool"] = threading.get_ident()
        return common.ok()

    @common.safe
    async def async_tool() -> common.Result:
        seen["loop"] = threading.get_ident()
        seen["blocking"] = await common.blocking(threading.get_ident)
        raise LookupError("not here")

    async def main() -> tuple[common.Result, common.Result]:
        return await tool(), await async_tool()

    first, second = anyio.run(main)
    assert first == {"ok": True}
    assert second == {"ok": False, "error": "not here"}
    assert seen["tool"] != seen["loop"] and seen["blocking"] != seen["loop"]


def test_safe_hides_unexpected_errors() -> None:
    @common.safe
    def broken() -> common.Result:
        raise RuntimeError("secret detail")

    assert anyio.run(broken) == {"ok": False, "error": "internal error: RuntimeError"}
