from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import synth

from wa_mcp import config, db, people


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("WA_MCP_CONFIG", "WA_MCP_STORE_DIR", "WA_MCP_API_TOKEN", "WA_MCP_SENDING_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    path = synth.make_config(tmp_path, people={"aliases": {"15550100003": "Bob the Builder"}})
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    synth.build_session_db(tmp_path / "store" / "session.db")
    return path


@pytest.fixture
def store(cfg_path: Path) -> Iterator[db.Store]:
    s = db.connect(config.load(cfg_path))
    people.connect(store=s)
    yield s
