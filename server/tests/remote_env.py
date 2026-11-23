"""Helpers for the remote-mode tests: a synthetic store, the wa-mcp CLI and a real HTTP server process."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python" / "tests"))

import synth
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PASSWORD = "correct horse battery staple"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def command() -> list[str]:
    exe = shutil.which("wa-mcp", path=str(Path(sys.executable).parent))
    return [exe] if exe else [sys.executable, "-m", "wa_mcp_server"]


def environ(cfg: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WA_MCP_")}
    env.update(WA_MCP_CONFIG=str(cfg), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


def make_store(root: Path, port: int, server: dict[str, Any]) -> Path:
    """config.json with a server section, a synthetic wa.db and no reachable bridge."""
    cfg = synth.make_config(root, port=free_port(), server={"listen": f"127.0.0.1:{port}", **server})
    synth.build_wa_db(root / "store" / "wa.db")
    synth.build_session_db(root / "store" / "session.db")
    return cfg


def cli(cfg: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*command(), *args],
        env=env or environ(cfg),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def create_token(cfg: Path, name: str) -> str:
    out = cli(cfg, "token", "create", "--name", name)
    assert out.returncode == 0, out.stderr
    return next(w for w in out.stdout.split() if w.startswith("wam_"))


@dataclass
class Server:
    base: str
    cfg: Path
    log: Path
    proc: subprocess.Popen[bytes]

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


@contextlib.contextmanager
def running(cfg: Path, port: int, *args: str, env: dict[str, str] | None = None) -> Iterator[Server]:
    """Start `wa-mcp serve --http` and stop it (by its own PID) afterwards."""
    log = cfg.parent / f"server-{port}.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [*command(), "serve", "--http", *args],
            env=env or environ(cfg),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    server = Server(base=f"http://127.0.0.1:{port}", cfg=cfg, log=log, proc=proc)
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"server stopped: {server.log_text()}")
            try:
                if httpx.get(server.base + "/health", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"server did not start: {server.log_text()}")
            time.sleep(0.2)
        yield server
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)


async def mcp_session(url: str, auth: httpx.Auth | None = None, token: str = "") -> dict[str, Any]:
    """initialize, list the tools and call list_chats over Streamable HTTP."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx.AsyncClient(headers=headers, auth=auth, timeout=60, trust_env=False) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        res = await session.call_tool("list_chats", {}, read_timeout_seconds=timedelta(seconds=60))
        data = json.loads("".join(getattr(c, "text", "") for c in res.content))
    return {"server": init.serverInfo.name, "tools": tools, "list_chats": data}
