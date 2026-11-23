"""OAuth 2.1 end to end against a real `wa-mcp serve --http`: discovery, dynamic registration,
authorization code with PKCE, consent (admin password and one-time approval link), token, refresh
rotation, revocation, and the MCP SDK's own OAuth client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import remote_env
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

REDIRECT = "http://127.0.0.1:33418/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _oauth_server(root: Path, approval: str) -> Iterator[remote_env.Server]:
    port = remote_env.free_port()
    oauth = {"enabled": True, "approval": approval, "access_token_minutes": 5}
    limits = {
        "per_token_per_minute": 1000,
        "per_ip_per_minute": 1000,
        "auth_per_ip_per_minute": 1000,
        "registrations_per_ip_per_hour": 1000,
    }
    cfg = remote_env.make_store(root, port, {"oauth": oauth, "rate_limit": limits})
    env = remote_env.environ(cfg, WA_MCP_ADMIN_PASSWORD=remote_env.PASSWORD)
    with remote_env.running(cfg, port, env=env) as srv:
        yield srv


@pytest.fixture(scope="module")
def srv(tmp_path_factory: pytest.TempPathFactory) -> Iterator[remote_env.Server]:
    yield from _oauth_server(tmp_path_factory.mktemp("oauth-pw"), "password")


@pytest.fixture(scope="module")
def link_srv(tmp_path_factory: pytest.TempPathFactory) -> Iterator[remote_env.Server]:
    yield from _oauth_server(tmp_path_factory.mktemp("oauth-link"), "link")


def _browser() -> httpx.Client:
    return httpx.Client(follow_redirects=False, timeout=30, trust_env=False)


def _register(srv: remote_env.Server, **extra: Any) -> dict[str, Any]:
    body = {
        "client_name": "Test app",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        **extra,
    }
    r = httpx.post(srv.base + "/register", json=body, trust_env=False)
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


def _authorize(
    browser: httpx.Client, srv: remote_env.Server, client: dict[str, Any], **extra: str
) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT,
        "state": "st-" + secrets.token_hex(4),
        "resource": srv.base + "/mcp",
        **extra,
    }
    return browser.get(srv.base + "/authorize", params=params)


def _consent_form(page: str) -> dict[str, str]:
    return dict(re.findall(r'name="(request|csrf)" value="([^"]+)"', page))


def _approve_with_password(
    srv: remote_env.Server, client: dict[str, Any], password: str = remote_env.PASSWORD
) -> tuple[str, str, str]:
    """Run the browser part; returns (code, state sent, verifier)."""
    verifier, challenge = _pkce()
    with _browser() as browser:
        r = _authorize(browser, srv, client, code_challenge=challenge, code_challenge_method="S256")
        assert r.status_code == 302, r.text
        state = parse_qs(urlsplit(str(r.request.url)).query)["state"][0]
        page = browser.get(r.headers["location"])
        assert page.status_code == 200 and "Admin password" in page.text and "Test app" in page.text
        assert "form-action 'self' http://127.0.0.1:33418" in page.headers["content-security-policy"]
        form = _consent_form(page.text)
        done = browser.post(
            srv.base + "/oauth/consent",
            data={**form, "password": password, "action": "approve"},
            headers={"Origin": srv.base},
        )
    assert done.status_code == 302, done.text
    query = parse_qs(urlsplit(done.headers["location"]).query)
    assert done.headers["location"].startswith(REDIRECT) and query["state"] == [state]
    return query["code"][0], state, verifier


def _token(srv: remote_env.Server, client: dict[str, Any], **data: str) -> httpx.Response:
    return httpx.post(srv.base + "/token", data={"client_id": client["client_id"], **data}, trust_env=False)


def _code_grant(srv: remote_env.Server, client: dict[str, Any], code: str, verifier: str) -> httpx.Response:
    return _token(
        srv, client, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, code_verifier=verifier
    )


def _mcp_status(srv: remote_env.Server, token: str) -> int:
    r = httpx.post(
        srv.base + "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        content=(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
            b'"capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
        ),
        trust_env=False,
    )
    return r.status_code


def test_discovery_metadata(srv: remote_env.Server) -> None:
    r = httpx.post(srv.base + "/mcp", json={}, trust_env=False)
    assert r.status_code == 401
    prm_url = f"{srv.base}/.well-known/oauth-protected-resource/mcp"
    assert f'resource_metadata="{prm_url}"' in r.headers["www-authenticate"]
    prm = httpx.get(prm_url, trust_env=False).json()
    assert prm["resource"] == srv.base + "/mcp" and prm["authorization_servers"] == [srv.base + "/"]
    meta = httpx.get(srv.base + "/.well-known/oauth-authorization-server", trust_env=False)
    assert "access-control-allow-origin" not in meta.headers
    m = meta.json()
    assert m["issuer"] == srv.base + "/" and m["code_challenge_methods_supported"] == ["S256"]
    assert (
        m["registration_endpoint"] == srv.base + "/register"
        and m["revocation_endpoint"] == srv.base + "/revoke"
    )
    assert m["grant_types_supported"] == ["authorization_code", "refresh_token"]


def test_registration_limits(srv: remote_env.Server) -> None:
    for uris in (["http://wa.example.test/cb"], ["javascript:alert(1)"], [REDIRECT] * 6):
        r = httpx.post(srv.base + "/register", json={"redirect_uris": uris}, trust_env=False)
        assert r.status_code == 400 and r.json()["error"] in (
            "invalid_redirect_uri",
            "invalid_client_metadata",
        )
    r = httpx.post(
        srv.base + "/register",
        json={
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token", "client_credentials"],
        },
        trust_env=False,
    )
    assert r.status_code == 400


def test_full_flow_with_rotation_and_revocation(srv: remote_env.Server) -> None:
    client = _register(srv)
    code, _, verifier = _approve_with_password(srv, client)
    assert _code_grant(srv, client, code, "wrong-" + verifier).json()["error"] == "invalid_grant"
    r = _code_grant(srv, client, code, verifier)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert (
        tok["token_type"] == "Bearer" and tok["expires_in"] == 300 and tok["access_token"].startswith("wao_")
    )
    assert r.headers["cache-control"] == "no-store"
    out = asyncio.run(remote_env.mcp_session(srv.base + "/mcp", token=tok["access_token"]))
    assert out["list_chats"]["ok"] is True

    # Refresh rotates both tokens; the old refresh token is spent.
    r2 = _token(srv, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    assert r2.status_code == 200, r2.text
    tok2 = r2.json()
    assert tok2["refresh_token"] != tok["refresh_token"] and tok2["access_token"] != tok["access_token"]
    assert _mcp_status(srv, tok2["access_token"]) == 200

    # Revocation ends the grant: access and refresh tokens stop working.
    rv = httpx.post(
        srv.base + "/revoke",
        data={"client_id": client["client_id"], "token": tok2["access_token"]},
        trust_env=False,
    )
    assert rv.status_code == 200
    assert _mcp_status(srv, tok2["access_token"]) == 401
    r3 = _token(srv, client, grant_type="refresh_token", refresh_token=tok2["refresh_token"])
    assert r3.json()["error"] == "invalid_grant"


def test_reused_refresh_token_revokes_the_grant(srv: remote_env.Server) -> None:
    client = _register(srv)
    code, _, verifier = _approve_with_password(srv, client)
    tok = _code_grant(srv, client, code, verifier).json()
    tok2 = _token(srv, client, grant_type="refresh_token", refresh_token=tok["refresh_token"]).json()
    again = _token(srv, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert _mcp_status(srv, tok2["access_token"]) == 401


def test_reused_code_revokes_its_tokens(srv: remote_env.Server) -> None:
    client = _register(srv)
    code, _, verifier = _approve_with_password(srv, client)
    tok = _code_grant(srv, client, code, verifier).json()
    assert _mcp_status(srv, tok["access_token"]) == 200
    assert _code_grant(srv, client, code, verifier).json()["error"] == "invalid_grant"
    assert _mcp_status(srv, tok["access_token"]) == 401


def test_basic_client_uses_header_for_token_and_revocation(srv: remote_env.Server) -> None:
    client = _register(srv, token_endpoint_auth_method="client_secret_basic")
    auth = (client["client_id"], client["client_secret"])
    code, _, verifier = _approve_with_password(srv, client)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }
    with _browser() as browser:
        assert browser.post(srv.base + "/token", data=data, auth=(auth[0], "incorrect")).status_code == 401
        assert (
            browser.post(srv.base + "/token", data={**data, "client_id": "other"}, auth=auth).status_code
            == 401
        )
        response = browser.post(srv.base + "/token", data=data, auth=auth)
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        assert browser.post(srv.base + "/revoke", data={"token": token}, auth=auth).status_code == 200
        assert _mcp_status(srv, token) == 401
        with closing(sqlite3.connect(srv.cfg.parent / "store" / "server.db")) as conn:
            conn.execute(
                "UPDATE oauth_clients SET info=json_set(info, '$.client_secret_expires_at', 1) WHERE client_id=?",
                (auth[0],),
            )
            conn.commit()
        assert browser.post(srv.base + "/revoke", data={"token": token}, auth=auth).status_code == 401


def test_confidential_client_secret(srv: remote_env.Server) -> None:
    client = _register(srv, token_endpoint_auth_method="client_secret_post")
    assert client["client_secret"]
    code, _, verifier = _approve_with_password(srv, client)
    assert _code_grant(srv, client, code, verifier).status_code == 401
    r = _token(
        srv,
        client,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        client_secret=client["client_secret"],
    )
    assert r.status_code == 200, r.text
    db = srv.cfg.parent / "store" / "server.db"
    conn = sqlite3.connect(db)
    rows = [
        str(row)
        for t in ("oauth_clients", "oauth_tokens", "oauth_codes")
        for row in conn.execute(f"SELECT * FROM {t}")
    ]
    conn.close()
    secrets_seen = [client["client_secret"], r.json()["access_token"], r.json()["refresh_token"], code]
    assert not any(s in row for s in secrets_seen for row in rows)


def test_authorize_refusals(srv: remote_env.Server) -> None:
    client = _register(srv)
    _, challenge = _pkce()
    with _browser() as browser:
        # A client that was never approved gets errors here, not on its redirect URI (no open redirect).
        plain = _authorize(browser, srv, client, code_challenge=challenge, code_challenge_method="plain")
        assert plain.status_code == 400 and "location" not in plain.headers
        assert plain.json()["error"] == "invalid_request"
        missing = _authorize(browser, srv, client)
        assert missing.status_code == 400 and "code_challenge" in missing.text
        wrong_uri = _authorize(
            browser, srv, client, code_challenge=challenge, redirect_uri="http://127.0.0.1:33418/other"
        )
        assert wrong_uri.status_code == 400 and "not registered" in wrong_uri.text
        other_resource = _authorize(
            browser, srv, client, code_challenge=challenge, resource="https://other.example.test/mcp"
        )
        assert other_resource.status_code == 400 and "unknown resource" in other_resource.text
        unknown = browser.get(
            srv.base + "/authorize",
            params={"client_id": "nope", "response_type": "code", "code_challenge": challenge},
        )
        assert unknown.status_code == 400


def test_consent_wrong_password_deny_and_other_browser(srv: remote_env.Server) -> None:
    client = _register(srv)
    _, challenge = _pkce()
    with _browser() as browser:
        r = _authorize(browser, srv, client, code_challenge=challenge)
        consent_url = r.headers["location"]
        page = browser.get(consent_url)
        form = _consent_form(page.text)
        # A second browser without the cookie cannot use the request.
        with _browser() as other:
            assert other.get(consent_url).status_code == 403
        bad = browser.post(
            srv.base + "/oauth/consent", data={**form, "password": "nope", "action": "approve"}
        )
        assert bad.status_code == 401 and "Wrong password" in bad.text
        forged = browser.post(srv.base + "/oauth/consent", data={**form, "csrf": "x", "action": "approve"})
        assert forged.status_code == 400
        assert browser.post(consent_url, data={**form, "csrf": "\u00ff"}).status_code == 400
        denied = browser.post(srv.base + "/oauth/consent", data={**form, "action": "deny"})
    assert denied.status_code == 302 and "error=access_denied" in denied.headers["location"]
    assert "state=st-" in denied.headers["location"]
    assert "wrong admin password" in srv.log_text()


def test_password_mode_disables_link_approval(srv: remote_env.Server) -> None:
    assert httpx.get(srv.base + "/oauth/approve", trust_env=False).status_code == 403


def test_approval_link_mode(link_srv: remote_env.Server) -> None:
    srv = link_srv
    client = _register(srv)
    verifier, challenge = _pkce()
    with _browser() as browser:
        r = _authorize(browser, srv, client, code_challenge=challenge)
        waiting = browser.get(r.headers["location"])
        assert (
            waiting.status_code == 200
            and 'http-equiv="refresh"' in waiting.text
            and "password" not in waiting.text
        )
        code_shown = re.search(r'class="req">([A-Z0-9]{6})<', waiting.text)
        assert code_shown is not None
        # The log carries the approval link; the CLI prints a fresh one and the old one stops working.
        logged = re.search(r"(http://127\.0\.0\.1:\d+/oauth/approve\?ticket=[\w-]+)", srv.log_text())
        assert logged is not None and code_shown.group(1) in srv.log_text()
        out = remote_env.cli(srv.cfg, "oauth", "pending")
        assert out.returncode == 0 and code_shown.group(1) in out.stdout
        fresh = re.search(r"approve: (\S+)", out.stdout)
        assert fresh is not None
        with _browser() as owner:
            assert owner.get(logged.group(1)).status_code == 404
            page = owner.get(fresh.group(1))
            assert page.status_code == 200 and "Allow" in page.text
            ticket = re.search(r'name="ticket" value="([^"]+)"', page.text)
            assert ticket is not None
            ok = owner.post(
                srv.base + "/oauth/approve", data={"ticket": ticket.group(1), "action": "approve"}
            )
            assert ok.status_code == 200 and "Approved" in ok.text
            assert owner.get(fresh.group(1)).status_code == 404
        # The waiting browser continues with the code on its next reload.
        done = browser.get(r.headers["location"])
    assert done.status_code == 302 and done.headers["location"].startswith(REDIRECT + "?code=")
    code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
    tok = _code_grant(srv, client, code, verifier)
    assert tok.status_code == 200 and _mcp_status(srv, tok.json()["access_token"]) == 200
    listed = remote_env.cli(srv.cfg, "oauth", "list").stdout
    assert client["client_id"] in listed and "approved, 1 active grant" in listed
    revoked = remote_env.cli(srv.cfg, "oauth", "revoke", client["client_id"])
    assert revoked.returncode == 0 and _mcp_status(srv, tok.json()["access_token"]) == 401


class _Memory:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


def test_mcp_sdk_oauth_client(srv: remote_env.Server) -> None:
    """The MCP Python SDK client discovers, registers, authorizes and connects on its own."""
    result: dict[str, str] = {}

    async def redirect(url: str) -> None:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30, trust_env=False) as browser:
            r = await browser.get(url)
            page = await browser.get(r.headers["location"])
            form = _consent_form(page.text)
            done = await browser.post(
                srv.base + "/oauth/consent",
                data={**form, "password": remote_env.PASSWORD, "action": "approve"},
            )
        q = parse_qs(urlsplit(done.headers["location"]).query)
        result.update(code=q["code"][0], state=q["state"][0])

    async def callback() -> tuple[str, str | None]:
        return result["code"], result["state"]

    storage = _Memory()
    auth = OAuthClientProvider(
        server_url=srv.base + "/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="SDK client", redirect_uris=[AnyUrl(REDIRECT)], token_endpoint_auth_method="none"
        ),
        storage=storage,
        redirect_handler=redirect,
        callback_handler=callback,
    )
    out = asyncio.run(remote_env.mcp_session(srv.base + "/mcp", auth=auth))
    assert out["list_chats"]["ok"] is True and "list_chats" in out["tools"]
    assert storage.tokens is not None and storage.tokens.access_token.startswith("wao_")


def test_default_redirect_prefixes_and_consent_details(srv: remote_env.Server) -> None:
    for uri in ("https://evil.example.test/callback", "cursor://anysphere.cursor-retrieval/oauth/callback"):
        r = httpx.post(srv.base + "/register", json={"redirect_uris": [uri]}, trust_env=False)
        assert r.status_code == 400 and "redirect_uri_prefixes" in r.json()["error_description"], uri
    claude = "https://claude.ai/api/mcp/auth_callback"
    r = httpx.post(
        srv.base + "/register", json={"client_name": "Claude", "redirect_uris": [claude]}, trust_env=False
    )
    assert r.status_code == 201
    client = _register(srv, client_name="Laptop app")
    _, challenge = _pkce()
    with _browser() as browser:
        page = browser.get(_authorize(browser, srv, client, code_challenge=challenge).headers["location"])
    assert (
        "Registered" in page.text and "from 127.0.0.1" in page.text and "never approved before" in page.text
    )
    assert "registered from 127.0.0.1: Laptop app" in srv.log_text()


def test_approved_client_gets_error_redirects(srv: remote_env.Server) -> None:
    client = _register(srv)
    code, _, verifier = _approve_with_password(srv, client)
    assert _code_grant(srv, client, code, verifier).status_code == 200
    _, challenge = _pkce()
    with _browser() as browser:
        r = _authorize(browser, srv, client, code_challenge=challenge, code_challenge_method="plain")
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT + "?error=invalid_request")
