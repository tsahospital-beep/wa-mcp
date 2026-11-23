"""wa-mcp subcommands for remote mode: serve, token, oauth, health."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime

import httpx

from wa_mcp import config as config_mod
from wa_mcp.db import StoreError
from wa_mcp_server.remote import settings as settings_mod
from wa_mcp_server.remote.oauth import Provider, describe_client
from wa_mcp_server.remote.store import ServerStore

log = logging.getLogger("wa_mcp_server.remote")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to config.json (default: env WA_MCP_CONFIG or ./config.json)")
    p = argparse.ArgumentParser(prog="wa-mcp", description="wa-mcp remote mode")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio, or HTTP with --http)")
    serve.add_argument("--toolsets", help="comma-separated MCP toolsets, or all")
    serve.add_argument("--http", action="store_true", help="serve MCP over Streamable HTTP instead of stdio")
    serve.add_argument("--listen", help="host:port for --http (default: server.listen, 127.0.0.1:8765)")
    serve.add_argument(
        "--insecure-lan",
        action="store_true",
        help="allow plain HTTP on a non-loopback address without a TLS proxy",
    )
    serve.add_argument("--log-level", default="")

    token = sub.add_parser("token", help="static bearer tokens for remote clients")
    tsub = token.add_subparsers(dest="action", required=True)
    create = tsub.add_parser("create", parents=[common], help="create a token; it is shown once")
    create.add_argument("--name", required=True, help="a label such as laptop")
    tsub.add_parser("list", parents=[common], help="list tokens (the values are never shown again)")
    revoke = tsub.add_parser("revoke", parents=[common], help="revoke a token by id or name")
    revoke.add_argument("token", help="id (tok_...) or name")

    oauth = sub.add_parser("oauth", help="OAuth clients and pending approvals")
    osub = oauth.add_subparsers(dest="action", required=True)
    osub.add_parser("list", parents=[common], help="list registered clients")
    orevoke = osub.add_parser("revoke", parents=[common], help="remove a client and revoke its tokens")
    orevoke.add_argument("client_id")
    osub.add_parser("pending", parents=[common], help="print fresh approval links for waiting requests")

    health = sub.add_parser(
        "health", parents=[common], help="exit 0 when the HTTP server and the bridge answer /health"
    )
    health.add_argument("--server-only", action="store_true", help="do not check the bridge")
    return p


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(list(argv))
    try:
        cfg = config_mod.load(args.config)
        if args.command == "serve":
            return _serve(cfg, args)
        remote = settings_mod.load(cfg)
        if args.command == "health":
            return _health(remote, None if args.server_only else cfg.api.listen)
        store = ServerStore.open(cfg.store_dir)
        if args.command == "token":
            return _token(store, args)
        return _oauth(store, remote, args)
    except (config_mod.ConfigError, StoreError, ValueError) as exc:
        sys.stderr.write(f"wa-mcp: {exc}\n")
        return 2 if isinstance(exc, config_mod.ConfigError | settings_mod.TransportRefused) else 1


def _serve(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from wa_mcp_server import app

    level = (args.log_level or ("INFO" if args.http else "WARNING")).upper()
    app.setup_logging(level, "%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.http:
        app.create_app(cfg, args.toolsets).run("stdio")
        return 0
    from wa_mcp_server.remote import http

    remote = settings_mod.load(cfg, args.listen)
    warning = settings_mod.check_transport(remote, args.insecure_lan)
    if warning:
        log.warning("%s", warning)
    password_needed = remote.oauth.enabled and remote.oauth.approval == "password"
    if password_needed and not settings_mod.secret_env(remote.oauth.admin_password_env):
        raise config_mod.ConfigError(
            f"server.oauth.approval is password but {remote.oauth.admin_password_env} "
            f"(or {remote.oauth.admin_password_env}_FILE) is empty"
        )
    store = ServerStore.open(cfg.store_dir)
    http.serve(
        app.create_app(cfg, args.toolsets),
        remote,
        store,
        settings_mod.restrict_peers(remote, args.insecure_lan),
    )
    return 0


def _token(store: ServerStore, args: argparse.Namespace) -> int:
    if args.action == "create":
        info, secret = store.create_token(args.name)
        _out(f"Created token {info.id} ({info.name}). It is shown only now; store it in your client:")
        _out("")
        _out(f"  {secret}")
        _out("")
        _out(f"Send it as the header  Authorization: Bearer {secret[:8]}...")
        return 0
    if args.action == "list":
        tokens = store.list_tokens()
        if not tokens:
            _out("no tokens; create one with: wa-mcp token create --name laptop")
        for t in tokens:
            state = "active" if t.active else f"revoked {_when(t.revoked_at)}"
            _out(
                f"{t.id}  {t.name:<20}  created {_when(t.created_at)}  last used {_when(t.last_used_at)}  {state}"
            )
        return 0
    revoked = store.revoke_token(args.token)
    if revoked is None:
        sys.stderr.write(f"wa-mcp: no active token with id or name {args.token!r}\n")
        return 1
    _out(f"revoked {revoked.id} ({revoked.name})")
    return 0


def _oauth(store: ServerStore, remote: settings_mod.RemoteSettings, args: argparse.Namespace) -> int:
    if args.action == "list":
        clients = store.list_clients()
        if not clients:
            _out("no OAuth clients registered")
        for c in clients:
            if c.revoked_at:
                state = f"revoked {_when(c.revoked_at)}"
            elif c.approved_at:
                state = f"approved, {store.active_grants(c.client_id)} active grant(s), last used {_when(c.last_used_at)}"
            else:
                state = "registered, never approved"
            _out(f"{c.client_id}  {describe_client(c.info)}  {state}")
        return 0
    if args.action == "revoke":
        if not store.revoke_client(args.client_id):
            sys.stderr.write(f"wa-mcp: no active client {args.client_id!r}\n")
            return 1
        _out(f"revoked client {args.client_id} and all its tokens")
        return 0
    pending = store.pending_requests()
    if not pending:
        _out("no requests are waiting for approval")
        return 0
    provider = Provider(store, remote)
    for rec in pending:
        client = store.get_client(rec.client_id)
        name = describe_client(client.info) if client else rec.client_id
        if remote.oauth.approval == "password":
            _out(f"request {rec.code}: {name}; approve with the admin password on the consent page")
        else:
            _out(f"request {rec.code}: {name}\n  approve: {provider.new_ticket(rec)}")
    return 0


def _probe(name: str, host: str, port: int) -> str:
    """Error text when http://host:port/health does not answer 200 with ok, "" when it does."""
    if settings_mod.is_unspecified(host):
        host = "127.0.0.1"
    url = f"http://{settings_mod.bracket(host)}:{port}/health"
    try:
        r = httpx.get(url, timeout=5.0, trust_env=False)
    except httpx.HTTPError as exc:
        return f"{name} at {url} does not answer: {exc.__class__.__name__}"
    try:
        ok = r.status_code == 200 and r.json().get("ok") is True
    except ValueError:
        ok = False
    return "" if ok else f"{name} at {url} answered {r.status_code}"


def _health(remote: settings_mod.RemoteSettings, bridge_listen: str | None) -> int:
    """The HTTP server, and the bridge API (its /health needs no token) unless bridge_listen is None."""
    problems = [_probe("the MCP server", remote.listen_host, remote.listen_port)]
    if bridge_listen is not None:
        host, port = settings_mod.split_listen(bridge_listen, "api.listen")
        problems.append(_probe("the bridge", host, port))
    failed = [p for p in problems if p]
    for p in failed:
        sys.stderr.write(f"wa-mcp: {p}\n")
    if failed:
        return 1
    _out("ok" if bridge_listen is None else "ok (server and bridge)")
    return 0
