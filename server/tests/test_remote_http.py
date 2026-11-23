"""Remote mode through the real interface: `wa-mcp serve --http` with static bearer tokens."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import remote_env

INIT = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
    b'"capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("remote-http")
    port = remote_env.free_port()
    cfg = remote_env.make_store(root, port, {"max_request_kb": 16})
    tokens = {name: remote_env.create_token(cfg, name) for name in ("laptop", "phone", "old")}
    with remote_env.running(cfg, port) as srv:
        yield {"srv": srv, "cfg": cfg, "tokens": tokens, "root": root}


def _post(srv: remote_env.Server, token: str = "", **kw: Any) -> httpx.Response:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(kw.pop("headers", {}))
    body = kw.pop("content", INIT)
    return httpx.post(srv.base + "/mcp", headers=headers, content=body, timeout=30, trust_env=False, **kw)


def test_mcp_over_http_with_a_token(server: dict[str, Any]) -> None:
    out = asyncio.run(remote_env.mcp_session(server["srv"].base + "/mcp", token=server["tokens"]["laptop"]))
    assert out["server"] == "wa-mcp"
    assert {"list_chats", "list_messages", "send_message", "bridge_status"} <= out["tools"]
    assert out["list_chats"]["ok"] is True and out["list_chats"]["chats"]


def test_without_or_with_a_wrong_token_401(server: dict[str, Any]) -> None:
    srv = server["srv"]
    for token in ("", "wam_" + "x" * 43, "not-a-token"):
        r = _post(srv, token)
        assert r.status_code == 401, token
        assert r.headers["www-authenticate"].startswith("Bearer")
    # OAuth is off: no metadata endpoints and no resource_metadata hint.
    assert "resource_metadata" not in _post(srv).headers["www-authenticate"]
    assert httpx.get(srv.base + "/.well-known/oauth-authorization-server", trust_env=False).status_code == 404


def test_revoked_token_401(server: dict[str, Any]) -> None:
    srv, token = server["srv"], server["tokens"]["old"]
    assert asyncio.run(remote_env.mcp_session(srv.base + "/mcp", token=token))["list_chats"]["ok"]
    out = remote_env.cli(server["cfg"], "token", "revoke", "old")
    assert out.returncode == 0 and "revoked" in out.stdout
    assert _post(srv, token).status_code == 401
    listed = remote_env.cli(server["cfg"], "token", "list").stdout
    assert "laptop" in listed and "revoked" in listed and "wam_" not in listed


def test_tokens_are_stored_hashed(server: dict[str, Any]) -> None:
    db = server["root"] / "store" / "server.db"
    raw = db.read_bytes() + (
        db.with_name("server.db-wal").read_bytes() if db.with_name("server.db-wal").exists() else b""
    )
    for token in server["tokens"].values():
        assert token.encode() not in raw and token[4:].encode() not in raw
    conn = sqlite3.connect(db)
    hashes = [r[0] for r in conn.execute("SELECT hash FROM tokens")]
    conn.close()
    assert len(hashes) == 3 and all(len(h) == 64 for h in hashes)
    assert (server["root"] / "store" / "server.key").is_file()


def test_host_and_origin_allow_lists(server: dict[str, Any]) -> None:
    srv, token = server["srv"], server["tokens"]["laptop"]
    assert _post(srv, token, headers={"Host": "evil.example.test"}).status_code == 421
    assert _post(srv, token, headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert _post(srv, token, headers={"Origin": "null"}).status_code == 403
    ok = _post(srv, token, headers={"Origin": srv.base})
    assert ok.status_code == 200, ok.text
    # /health answers through any port mapping and shows nothing.
    health = httpx.get(srv.base + "/health", headers={"Host": "127.0.0.1:1"}, trust_env=False)
    assert health.status_code == 200 and health.json() == {"ok": True}


def test_request_size_limit(server: dict[str, Any]) -> None:
    big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * 20000 + b'"}}'
    assert _post(server["srv"], server["tokens"]["laptop"], content=big).status_code == 413


def test_health_and_headers(server: dict[str, Any]) -> None:
    r = httpx.get(server["srv"].base + "/health", trust_env=False)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in r.headers
    pre = httpx.options(
        server["srv"].base + "/mcp",
        headers={"Origin": server["srv"].base, "Access-Control-Request-Method": "POST"},
        trust_env=False,
    )
    assert "access-control-allow-origin" not in pre.headers
    assert remote_env.cli(server["cfg"], "health", "--server-only").returncode == 0
    # Without --server-only the bridge is checked too; none runs here.
    both = remote_env.cli(server["cfg"], "health")
    assert both.returncode == 1 and "the bridge" in both.stderr and "the MCP server" not in both.stderr


def test_health_checks_the_bridge_too(tmp_path: Path) -> None:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"ok": true, "connected": false, "logged_in": false}'
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    bridge = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    try:
        port = remote_env.free_port()
        cfg = remote_env.make_store(tmp_path, port, {})
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        raw["api"]["listen"] = f"127.0.0.1:{bridge.server_address[1]}"
        cfg.write_text(json.dumps(raw), encoding="utf-8")
        with remote_env.running(cfg, port):
            out = remote_env.cli(cfg, "health")
        assert out.returncode == 0 and "server and bridge" in out.stdout
    finally:
        bridge.shutdown()


def test_access_log_has_no_secrets(server: dict[str, Any]) -> None:
    text = server["srv"].log_text()
    assert "http POST /mcp 200" in text
    for token in server["tokens"].values():
        assert token not in text


def test_https_guard_refuses_plain_lan(tmp_path: Path) -> None:
    port = remote_env.free_port()
    cfg = remote_env.make_store(tmp_path, port, {})
    out = remote_env.cli(cfg, "serve", "--http", "--listen", f"0.0.0.0:{port}")
    assert out.returncode == 2 and "refusing to serve plain HTTP" in out.stderr
    env = remote_env.environ(cfg, WA_MCP_SERVER_PUBLIC_URL="http://wa.example.test")
    out = remote_env.cli(cfg, "serve", "--http", env=env)
    assert out.returncode == 2 and "public_url must be https" in out.stderr


def test_insecure_lan_flag_warns(tmp_path: Path) -> None:
    port = remote_env.free_port()
    cfg = remote_env.make_store(tmp_path, port, {})
    with remote_env.running(cfg, port, "--listen", f"0.0.0.0:{port}", "--insecure-lan") as srv:
        assert "--insecure-lan: MCP over plain HTTP" in srv.log_text()


def test_https_public_url_needs_trusted_proxies(tmp_path: Path) -> None:
    port = remote_env.free_port()
    cfg = remote_env.make_store(tmp_path, port, {"public_url": "https://wa.example.test"})
    out = remote_env.cli(cfg, "serve", "--http", "--listen", f"0.0.0.0:{port}")
    assert out.returncode == 2 and "server.trusted_proxies" in out.stderr


def test_https_public_url_allows_any_interface(tmp_path: Path) -> None:
    port = remote_env.free_port()
    server = {"public_url": "https://wa.example.test", "trusted_proxies": ["10.0.0.0/8"]}
    cfg = remote_env.make_store(tmp_path, port, server)
    with remote_env.running(cfg, port, "--listen", f"0.0.0.0:{port}") as srv:
        assert "https://wa.example.test/mcp" in srv.log_text()
        assert "accepted only from loopback and server.trusted_proxies" in srv.log_text()
        token = remote_env.create_token(cfg, "proxy")
        r = _post(srv, token, headers={"Host": "wa.example.test", "Origin": "https://wa.example.test"})
        assert r.status_code == 200
