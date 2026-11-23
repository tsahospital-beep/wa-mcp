"""Remote mode units: settings and the HTTPS guard, rate limits, client address, redirect URI rules,
the token store, and the guard layer through an in-process app."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import sqlite3
import warnings
from pathlib import Path
from typing import Any

import pytest
import remote_env
from starlette.datastructures import Headers

from wa_mcp import config
from wa_mcp_server import app as app_mod
from wa_mcp_server.remote import http, settings
from wa_mcp_server.remote import store as store_mod
from wa_mcp_server.remote.guard import CLIENT_IP_KEY, Buckets, Guard, client_ip, log_path
from wa_mcp_server.remote.oauth import check_redirect_uri
from wa_mcp_server.remote.store import ServerStore

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette suggests another HTTP client package
    from starlette.testclient import TestClient


def _settings(tmp_path: Path, **server: Any) -> settings.RemoteSettings:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server": server}), encoding="utf-8")
    return settings.load(config.load(path))


def test_defaults(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert (s.listen_host, s.listen_port, s.public_url) == ("127.0.0.1", 8765, "")
    assert s.base_url == "http://127.0.0.1:8765" and s.resource_url == "http://127.0.0.1:8765/mcp"
    assert s.max_request_bytes == 256 * 1024 and s.oauth.enabled is False and s.oauth.approval == "link"
    assert s.rate == settings.RateLimits(120, 240, 20, 10)
    assert s.oauth.redirect_uri_prefixes[0] == "https://claude.ai/api/mcp/auth_callback"
    assert "127.0.0.1:8765" in s.hosts() and "localhost:8765" in s.hosts()
    assert s.origins() >= {"http://127.0.0.1:8765", "http://localhost:8765"}
    assert settings.check_transport(s, insecure_lan=False) == ""


def test_public_url_hosts_and_origins(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        listen="0.0.0.0:8765",
        public_url="https://WA.example.test/",
        allowed_origins=["https://app.example.test"],
        cors_origins=["https://inspector.example.test"],
        trusted_proxies=["172.16.0.0/12"],
    )
    assert s.base_url == "https://wa.example.test" and s.resource_url == "https://wa.example.test/mcp"
    assert {"wa.example.test", "wa.example.test:443", "127.0.0.1:8765"} <= s.hosts()
    assert "0.0.0.0:8765" not in s.hosts()
    assert s.origins() == {
        "https://wa.example.test",
        "https://app.example.test",
        "https://inspector.example.test",
    }
    assert "only from loopback and server.trusted_proxies" in settings.check_transport(s, insecure_lan=False)


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({"listen": "8765"}, "want host:port"),
        ({"public_url": "ftp://wa.example.test"}, "public_url"),
        ({"public_url": "https://wa.example.test/mcp"}, "root of its host"),
        ({"max_request_kb": 1}, "max_request_kb"),
        ({"oauth": {"approval": "email"}}, "approval"),
        ({"trusted_proxies": ["not-an-ip"]}, "trusted_proxies"),
        ({"oauth": {"redirect_uri_prefixes": ["https://"]}}, "redirect_uri_prefixes"),
        ({"oauth": {"redirect_uri_prefixes": ["https://[invalid"]}}, "redirect_uri_prefixes"),
        ({"allowed_origins": ["https://a.example.test/path"]}, "not an origin"),
        ({"rate_limit": {"per_ip_per_minute": -1}}, "per_ip_per_minute"),
    ],
)
def test_bad_values(tmp_path: Path, server: dict[str, Any], message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        _settings(tmp_path, **server)


def test_transport_guard(tmp_path: Path) -> None:
    lan = _settings(tmp_path, listen="192.0.2.10:8765")
    with pytest.raises(settings.TransportRefused, match="refusing to serve plain HTTP"):
        settings.check_transport(lan, insecure_lan=False)
    assert "--insecure-lan" in settings.check_transport(lan, insecure_lan=True)
    plain = _settings(tmp_path, listen="0.0.0.0:8765", public_url="http://wa.example.test")
    with pytest.raises(settings.TransportRefused, match="must be https"):
        settings.check_transport(plain, insecure_lan=False)
    oauth_lan = _settings(tmp_path, listen="192.0.2.10:8765", oauth={"enabled": True})
    with pytest.raises(settings.TransportRefused, match="oauth needs an https"):
        settings.check_transport(oauth_lan, insecure_lan=True)
    oauth_local = _settings(tmp_path, oauth={"enabled": True})
    assert settings.check_transport(oauth_local, insecure_lan=False) == ""
    # Behind a TLS proxy on a non-loopback address the proxy must be declared.
    proxy = _settings(tmp_path, listen="0.0.0.0:8765", public_url="https://wa.example.test")
    with pytest.raises(settings.TransportRefused, match="trusted_proxies"):
        settings.check_transport(proxy, insecure_lan=False)
    assert settings.check_transport(_settings(tmp_path, public_url="https://wa.example.test"), False) == ""


def test_env_overrides_and_secret_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WA_MCP_SERVER_LISTEN", "127.0.0.1:9911")
    monkeypatch.setenv("WA_MCP_SERVER_PUBLIC_URL", "https://wa.example.test")
    s = _settings(tmp_path)
    assert s.listen_port == 9911 and s.public_url == "https://wa.example.test"
    secret = tmp_path / "pw.txt"
    secret.write_text("from a file\n", encoding="utf-8")
    remote_env.synth.make_private(secret)
    monkeypatch.delenv("WA_MCP_TEST_SECRET", raising=False)
    monkeypatch.setenv("WA_MCP_TEST_SECRET_FILE", str(secret))
    assert settings.secret_env("WA_MCP_TEST_SECRET") == "from a file"
    monkeypatch.setenv("WA_MCP_TEST_SECRET", "direct")
    assert settings.secret_env("WA_MCP_TEST_SECRET") == "direct"
    monkeypatch.delenv("WA_MCP_TEST_SECRET")
    monkeypatch.setenv("WA_MCP_TEST_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(config.ConfigError, match="WA_MCP_TEST_SECRET_FILE"):
        settings.secret_env("WA_MCP_TEST_SECRET")


def test_buckets_refill() -> None:
    t = [0.0]
    b = Buckets(3, clock=lambda: t[0])
    assert [b.take("a") for _ in range(4)] == [True, True, True, False]
    assert b.take("b") and b.empty("a") and b.retry_after("a") >= 1
    t[0] += 20.0
    assert b.take("a") and not b.take("a")
    assert Buckets(0).take("x") and not Buckets(0).empty("x")


def test_client_ip_behind_trusted_proxy() -> None:
    nets = (ipaddress.ip_network("172.16.0.0/12"),)
    headers = Headers(raw=[(b"x-forwarded-for", b"203.0.113.7, 172.18.0.5")])
    assert client_ip({"client": ("172.18.0.3", 1)}, headers, nets) == "203.0.113.7"
    assert client_ip({"client": ("198.51.100.2", 1)}, headers, nets) == "198.51.100.2"
    assert client_ip({"client": ("172.18.0.3", 1)}, headers, ()) == "172.18.0.3"
    malformed = Headers(raw=[(b"x-forwarded-for", b"invalid")])
    assert client_ip({"client": ("172.18.0.3", 1)}, malformed, nets) == "172.18.0.3"


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://claude.ai/api/mcp/auth_callback", True),
        ("http://127.0.0.1:33418/callback", True),
        ("http://localhost:6274/oauth/callback", True),
        ("cursor://anysphere.cursor-retrieval/oauth/callback", True),
        ("http://wa.example.test/callback", False),
        ("javascript:alert(1)", False),
        ("https://claude.ai/cb#frag", False),
        ("https://" + "a" * 600 + ".test/", False),
        ("http://localhost:80@evil.example.test/cb", False),
        ("https://user@claude.ai/api/mcp/auth_callback", False),
    ],
)
def test_redirect_uri_rules(uri: str, ok: bool) -> None:
    assert (check_redirect_uri(uri, ()) is None) is ok
    defaults = tuple(config.DEFAULTS["server"]["oauth"]["redirect_uri_prefixes"])
    allowed_by_default = ok and not uri.startswith("cursor:")
    assert (check_redirect_uri(uri, defaults) is None) is allowed_by_default
    if ok and uri.startswith("https://claude.ai"):
        assert check_redirect_uri(uri, ("https://claude.ai/api/mcp/",)) is None
        assert check_redirect_uri(uri, ("https://other.example.test/",)) is not None


def test_token_store(tmp_path: Path) -> None:
    store = ServerStore.open(tmp_path / "auth")
    info, secret = store.create_token("laptop")
    assert secret.startswith("wam_") and len(secret) == 47
    with pytest.raises(ValueError, match="exists"):
        store.create_token("laptop")
    with pytest.raises(ValueError, match="token name"):
        store.create_token("bad/name")
    assert store.check_token(secret) == info
    assert store.check_token(secret[:-1] + ("A" if secret[-1] != "A" else "B")) is None
    assert store.check_token("wao_" + secret[4:]) is None
    assert store.revoke_token("laptop") is not None and store.check_token(secret) is None
    again, _ = store.create_token("laptop")
    assert again.id != info.id
    conn = sqlite3.connect(store.path)
    assert all(secret not in str(row) for row in conn.execute("SELECT * FROM tokens"))
    conn.close()
    # Another pepper gives other hashes, so a copied database alone does not verify tokens.
    other = ServerStore(store.path, b"x" * 32)
    assert other.check_token(secret) is None


def _client(tmp_path: Path, **server: Any) -> TestClient:
    port = remote_env.free_port()
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "store_dir": "auth",
                "api": {"listen": f"127.0.0.1:{remote_env.free_port()}", "token": "test-token-0123456789"},
                "server": {"listen": f"127.0.0.1:{port}", **server},
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load(cfg)
    remote = settings.load(loaded)
    store = ServerStore.open(loaded.store_dir)
    asgi = http.build_app(app_mod.create_app(loaded), remote, store)
    client = TestClient(asgi, base_url=f"http://127.0.0.1:{port}")
    client.store = store  # type: ignore[attr-defined]
    return client


INIT = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
    b'"capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
)


def _ping(client: TestClient, token: str = "", **headers: str) -> int:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=h, content=INIT).status_code


def test_rate_limit_per_token_and_failed_auth(tmp_path: Path) -> None:
    limits = {"per_token_per_minute": 3, "per_ip_per_minute": 100, "auth_per_ip_per_minute": 4}
    with _client(tmp_path, rate_limit=limits) as client:
        _, token = client.store.create_token("laptop")  # type: ignore[attr-defined]
        codes = [_ping(client, token) for _ in range(4)]
        # A valid token is limited by its own budget only.
        assert codes == [200, 200, 200, 429]
        r = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, content=b"{}")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        # Wrong tokens use up the failed-authentication budget of the address ...
        assert [_ping(client, f"wam_wrong{i}") for i in range(4)] == [401] * 4
        assert _ping(client, "wam_wrong9") == 429 and _ping(client) == 429
        # ... but a valid token from the same address still gets through.
        _, fresh = client.store.create_token("phone")  # type: ignore[attr-defined]
        assert _ping(client, fresh) == 200


def test_rate_limit_counts_failed_requests_only(tmp_path: Path) -> None:
    limits = {"per_token_per_minute": 0, "per_ip_per_minute": 5, "auth_per_ip_per_minute": 0}
    with _client(tmp_path, rate_limit=limits) as client:
        _, token = client.store.create_token("laptop")  # type: ignore[attr-defined]
        assert [_ping(client, token) for _ in range(8)] == [200] * 8  # successes never count
        codes = [client.get("/nothing").status_code for _ in range(6)]
        assert codes[:5] == [404] * 5 and codes[5] == 429
        assert client.get("/health").status_code == 200
        assert _ping(client, token) == 200


def test_forwarded_address_from_a_proxy_on_this_host(tmp_path: Path) -> None:
    """Listening on loopback behind an https public_url, X-Forwarded-For from loopback is trusted, so the
    limits, registrations and the consent backoff see the real client."""
    s = _settings(tmp_path, public_url="https://wa.example.test")
    assert [str(n) for n in s.proxies()] == ["127.0.0.0/8", "::1/128"]
    assert _settings(tmp_path).proxies() == ()
    assert [str(n) for n in _settings(tmp_path, trusted_proxies=["10.0.0.0/8"]).proxies()] == ["10.0.0.0/8"]
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope[CLIENT_IP_KEY])
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guard = Guard(app, s)
    for i in range(3):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "client": ("127.0.0.1", 50000),
            "headers": [(b"host", b"wa.example.test"), (b"x-forwarded-for", f"203.0.113.{i}".encode())],
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Any) -> None:
            return

        asyncio.run(guard(scope, receive, send))
    assert seen == ["203.0.113.0", "203.0.113.1", "203.0.113.2"]
    # The failures were counted per client, not on the proxy's address.
    assert not guard.failures.empty("127.0.0.1") and not guard.auth._state.get("127.0.0.1")


def test_access_log_escapes_the_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with _client(tmp_path) as client, caplog.at_level(logging.INFO, logger="wa_mcp_server.remote.http"):
        client.get("/x%0Afake%20200%20line")
    lines = [r.getMessage() for r in caplog.records if r.name == "wa_mcp_server.remote.http"]
    assert lines and all("\n" not in line for line in lines)
    assert any("/x%0Afake%20200%20line 404" in line for line in lines)
    scope = {"type": "http", "raw_path": b"/a\nb\x01c\xff", "path": "/a"}
    assert log_path(scope) == "/a\\x0ab\\x01c\\xff"


def test_cors_only_when_configured(tmp_path: Path) -> None:
    with _client(tmp_path, cors_origins=["https://inspector.example.test"]) as client:
        pre = client.options(
            "/mcp",
            headers={
                "Origin": "https://inspector.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == "https://inspector.example.test"
        other = client.options(
            "/mcp", headers={"Origin": "https://evil.example.test", "Access-Control-Request-Method": "POST"}
        )
        assert other.status_code == 403


def test_peers_outside_trusted_proxies_are_refused(tmp_path: Path) -> None:
    port = remote_env.free_port()
    server = {"public_url": "https://wa.example.test", "trusted_proxies": ["10.0.0.0/8"]}
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "store_dir": "auth",
                "api": {"listen": f"127.0.0.1:{remote_env.free_port()}", "token": "test-token-0123456789"},
                "server": {"listen": f"127.0.0.1:{port}", **server},
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load(cfg)
    remote = settings.load(loaded, f"0.0.0.0:{port}")
    assert settings.restrict_peers(remote, insecure_lan=False)
    assert not settings.restrict_peers(remote, insecure_lan=True)
    store = ServerStore.open(loaded.store_dir)
    asgi = http.build_app(app_mod.create_app(loaded), remote, store, restrict_peers=True)
    with TestClient(asgi, base_url="https://wa.example.test") as client:  # its peer is "testclient"
        assert client.post("/mcp", content=b"{}").status_code == 403
        assert client.get("/health").status_code == 200


def test_http_client_logs_no_urls(caplog: pytest.LogCaptureFixture) -> None:
    import httpx

    app_mod.setup_logging("INFO")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with caplog.at_level(logging.INFO), httpx.Client(transport=transport) as h:
        h.get("http://127.0.0.1:8080/api/v1/messages", params={"chat": "15550100002@s.whatsapp.net"})
    logged = [r for r in caplog.records if "s.whatsapp.net" in r.getMessage() or r.name.startswith("http")]
    assert not logged
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_registration_cap_counts_approved_clients_only(tmp_path: Path) -> None:
    store = ServerStore.open(tmp_path / "auth")
    info = {"redirect_uris": ["http://127.0.0.1:1/cb"]}
    for i in range(store_mod.PENDING_CLIENTS + 3):
        store.add_client(f"c{i}", info, None, "192.0.2.1", 2)
    pending = [c.client_id for c in store.list_clients() if c.approved_at is None]
    # Unapproved registrations are capped; the oldest are dropped first.
    assert len(pending) == store_mod.PENDING_CLIENTS and "c0" not in pending and "c12" in pending
    t = store_mod.now()
    for name, expected in [("c11", True), ("c12", True), ("c10", False)]:
        rec = store_mod.RequestRecord("r" + name, "code", name, {}, None, "ticket", "pending", t, t + 60)
        store.add_request(rec)
        assert not store.decide_request(rec, "approved", 2, "expired-ticket")
        assert store.decide_request(rec, "approved", 2, "ticket") == expected
        if not expected:
            assert store.decide_request(rec, "denied", 2, "ticket")
    with pytest.raises(ValueError, match="too many approved clients"):
        store.add_client("c99", info, None, "192.0.2.1", 2)
    store.revoke_client("c12")
    store.add_client("c99", info, None, "192.0.2.9", 2)
    rec = store.get_client("c99")
    assert rec is not None and rec.registered_ip == "192.0.2.9"
    # Unapproved clients expire after an hour.
    conn = sqlite3.connect(store.path)
    conn.execute(
        "UPDATE oauth_clients SET created_at = created_at - ? WHERE approved_at IS NULL",
        (store_mod.PENDING_CLIENT_SECONDS + 1,),
    )
    conn.commit()
    conn.close()
    store.add_client("fresh", info, None, "", 5)
    left = {c.client_id for c in store.list_clients() if c.approved_at is None and c.revoked_at is None}
    assert left == {"fresh"}


def _oauth_client(tmp_path: Path, **server: Any) -> TestClient:
    return _client(tmp_path, oauth={"enabled": True, "approval": "password"}, **server)


def _flow_to_consent(client: TestClient) -> dict[str, str]:
    import base64
    import hashlib
    import re
    import secrets

    redirect = "http://127.0.0.1:33418/callback"
    reg = client.post("/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none"})
    assert reg.status_code == 201, reg.text
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "response_type": "code",
        "client_id": reg.json()["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "state": "s1",
    }
    r = client.get("/authorize", params=params, follow_redirects=False)
    page = client.get(r.headers["location"], follow_redirects=False)
    return dict(re.findall(r'name="(request|csrf)" value="([^"]+)"', page.text))


def test_registration_limit_per_address(tmp_path: Path) -> None:
    limits = {
        "per_token_per_minute": 0,
        "per_ip_per_minute": 0,
        "auth_per_ip_per_minute": 0,
        "registrations_per_ip_per_hour": 2,
    }
    with _oauth_client(tmp_path, rate_limit=limits) as client:
        body = {"redirect_uris": ["http://127.0.0.1:33418/callback"]}
        codes = [client.post("/register", json=body).status_code for _ in range(3)]
        assert codes == [201, 201, 429]


def test_consent_password_backoff_per_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wa_mcp_server.remote import consent

    now = [1000.0]
    monkeypatch.setattr(consent, "CLOCK", lambda: now[0])
    monkeypatch.setenv("WA_MCP_ADMIN_PASSWORD", "right password")
    with _oauth_client(tmp_path) as client:
        form = _flow_to_consent(client)

        def attempt(password: str) -> int:
            data = {**form, "password": password, "action": "approve"}
            return client.post("/oauth/consent", data=data, follow_redirects=False).status_code

        assert [attempt("wrong") for _ in range(3)] == [401, 401, 401]
        # From the third failure on, this address waits, also with the right password.
        assert attempt("right password") == 429
        now[0] += consent.BACKOFF_SECONDS + 1
        assert attempt("wrong") == 401
        assert attempt("right password") == 429
        now[0] += 2 * consent.BACKOFF_SECONDS + 1
        assert attempt("right password") == 302


def test_consent_survives_registration_eviction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "PENDING_CLIENTS", 1)
    store = ServerStore.open(tmp_path / "auth")
    store.add_client("active", {}, None, "", 3)
    t = store_mod.now()
    req = store_mod.RequestRecord("r", "code", "active", {}, None, None, "pending", t, t + 30)
    store.add_request(req)
    with pytest.raises(ValueError, match="waiting for approval"):
        store.add_client("another", {}, None, "", 3)
    assert store.get_request("r") == req and store.get_client("active") is not None
    monkeypatch.setattr(store_mod, "now", lambda: t + 31)
    store.add_client("another", {}, None, "", 3)
    assert store.get_client("active") is None


def test_redirect_prefix_matches_origin(tmp_path: Path) -> None:
    s = _settings(tmp_path, oauth={"redirect_uri_prefixes": ["https://app.example.test"]})
    prefixes = s.oauth.redirect_uri_prefixes
    assert check_redirect_uri("https://app.example.test/return", prefixes) is None
    assert check_redirect_uri("https://app.example.test.other.test/return", prefixes)
    assert check_redirect_uri("https://app.example.test:invalid/return", prefixes)
    assert check_redirect_uri("https://[invalid", prefixes)


def test_tool_failure_keeps_partial_send_ids() -> None:
    from wa_mcp.client import BridgeError
    from wa_mcp_server import common

    result = common._error(
        "send_file", BridgeError(502, "send interrupted", {"partial": True, "ids": ["first"]})
    )
    assert result == {
        "ok": False,
        "error": "bridge answered 502: send interrupted",
        "partial": True,
        "ids": ["first"],
    }


def test_tools_check_account_files_before_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wa_mcp_server import common

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"store_dir": "private", "accounts": {"work": {"api": {"listen": "127.0.0.1:0"}}}}),
        encoding="utf-8",
    )
    parent = config.load(path)
    child = parent.for_account("work")
    child.downloads_dir.mkdir(parents=True)
    note = child.downloads_dir / "note.txt"
    note.write_text("note", encoding="utf-8")
    monkeypatch.setattr(common, "cfg", lambda: parent)
    for check in (common.sendable_file, common.importable_file):
        with pytest.raises(ValueError, match="protected"):
            check(str(note))
    monkeypatch.setattr(common, "cfg", lambda: child)
    assert common.sendable_file(str(note)) == note
    monkeypatch.setenv("WA_MCP_SERVER_PEPPER_FILE", str(note))
    for check in (common.sendable_file, common.importable_file):
        with pytest.raises(ValueError, match="protected"):
            check(str(note))
    monkeypatch.delenv("WA_MCP_SERVER_PEPPER_FILE")
    monkeypatch.setenv(child.encryption.passphrase_env + "_FILE", str(note))
    with pytest.raises(ValueError, match="protected"):
        common.importable_file(str(note))


def test_code_exchange_rechecks_replay_inside_transaction(tmp_path: Path) -> None:
    store = ServerStore.open(tmp_path)
    end = store_mod.now() + 60
    store.add_client("client", {}, None, "", 10)
    store.add_code("code", "client", {}, end)
    pair = store_mod.Issued("first", "access", end, "refresh", end)
    assert store.exchange_code("code", "client", ["mcp"], "resource", pair)
    assert not store.exchange_code("code", "other", ["mcp"], "resource", pair)
    assert store.active_grants("client") == 1
    assert not store.exchange_code("code", "client", ["mcp"], "resource", pair)
    assert store.active_grants("client") == 0


def test_concurrent_pepper_creation(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(8)

    def load(_: int) -> bytes:
        barrier.wait(timeout=10)
        return store_mod.load_pepper(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = set(pool.map(load, range(8)))
    assert keys == {(tmp_path / "server.key").read_bytes().strip()}
    assert len(next(iter(keys))) == 64
    assert [p.name for p in tmp_path.iterdir()] == ["server.key"]


def test_remote_nonfinite_request_limit(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="max_request_kb"):
        _settings(tmp_path, max_request_kb=float("inf"))
