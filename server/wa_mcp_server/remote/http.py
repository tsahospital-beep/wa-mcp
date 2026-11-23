"""The remote-mode ASGI app: MCP Streamable HTTP at /mcp behind bearer authentication, the OAuth 2.1
endpoints when server.oauth.enabled is true, and /health. Served with uvicorn."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from functools import partial
from typing import Any
from urllib.parse import parse_qs, urlsplit

import uvicorn
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from wa_mcp_server.remote.consent import Consent
from wa_mcp_server.remote.guard import CLIENT_IP_KEY, HEALTH_PATH, Guard
from wa_mcp_server.remote.oauth import (
    SCOPE,
    HashedClientAuthenticator,
    Provider,
    Verifier,
    registration_ip,
    revoke_endpoint,
    token_endpoint,
)
from wa_mcp_server.remote.sdk import (
    AuthContextMiddleware,
    AuthorizationHandler,
    BearerAuthBackend,
    ClientRegistrationOptions,
    FastMCP,
    MetadataHandler,
    ProtectedResourceMetadata,
    ProtectedResourceMetadataHandler,
    RegistrationHandler,
    RequestBodyLimitMiddleware,
    RequireAuthMiddleware,
    RevocationOptions,
    StreamableHTTPASGIApp,
    TokenHandler,
    TransportSecuritySettings,
    build_metadata,
    build_resource_metadata_url,
)
from wa_mcp_server.remote.settings import MCP_PATH, RemoteSettings
from wa_mcp_server.remote.store import ServerStore

log = logging.getLogger("wa_mcp_server.remote")
# Open MCP sessions at a time; idle sessions close after 30 minutes (the SDK default).
MAX_SESSIONS = 200


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True})


async def _register(request: Request, handler: RegistrationHandler) -> Response:
    """The SDK registration handler, with the client address passed on to the provider."""
    token = registration_ip.set(str(request.scope.get(CLIENT_IP_KEY) or ""))
    try:
        return await handler.handle(request)
    finally:
        registration_ip.reset(token)


async def _authorize(request: Request, handler: AuthorizationHandler, provider: Provider) -> Response:
    """The SDK authorization handler. Its error redirects go to the client's redirect URI; for a client
    the owner never approved they are answered here instead, so the server is no open redirect."""
    response = await handler.handle(request)
    location = response.headers.get("location", "")
    if response.status_code in (302, 303) and not location.startswith(
        provider.settings.base_url + "/oauth/consent?"
    ):
        params = request.query_params if request.method == "GET" else await request.form()
        client_id = params.get("client_id")
        rec = await provider.client_record(client_id) if isinstance(client_id, str) and client_id else None
        if rec is None or rec.approved_at is None:
            query = parse_qs(urlsplit(location).query)
            detail = query.get("error_description", ["the authorization request is not valid"])[0]
            return JSONResponse(
                {
                    "error": query.get("error", ["invalid_request"])[0],
                    "error_description": f"{detail} (errors of a client that was never approved are not sent "
                    "to its redirect URI)",
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
    return response


def _oauth_routes(provider: Provider, settings: RemoteSettings, resource: AnyHttpUrl) -> list[Route]:
    issuer = AnyHttpUrl(settings.base_url)
    registration = ClientRegistrationOptions(enabled=True, default_scopes=[SCOPE])
    metadata = build_metadata(issuer, None, registration, RevocationOptions(enabled=True))
    metadata.scopes_supported = [SCOPE]
    metadata.token_endpoint_auth_methods_supported = ["client_secret_post", "client_secret_basic", "none"]
    prm = ProtectedResourceMetadata(
        resource=resource, authorization_servers=[issuer], scopes_supported=[SCOPE], resource_name="WA MCP"
    )
    authn = HashedClientAuthenticator(provider)
    prm_handler = ProtectedResourceMetadataHandler(prm).handle
    prm_path = urlsplit(str(build_resource_metadata_url(resource))).path
    return [
        Route("/.well-known/oauth-authorization-server", MetadataHandler(metadata).handle, methods=["GET"]),
        Route(prm_path, prm_handler, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", prm_handler, methods=["GET"]),
        Route(
            "/authorize",
            partial(_authorize, handler=AuthorizationHandler(provider), provider=provider),
            methods=["GET", "POST"],
        ),
        Route("/token", partial(token_endpoint, handler=TokenHandler(provider, authn)), methods=["POST"]),
        Route(
            "/register",
            partial(_register, handler=RegistrationHandler(provider, options=registration)),
            methods=["POST"],
        ),
        Route("/revoke", partial(revoke_endpoint, provider=provider, authn=authn), methods=["POST"]),
        *Consent(provider).routes(),
    ]


def build_app(
    mcp: FastMCP, settings: RemoteSettings, store: ServerStore, restrict_peers: bool = False
) -> ASGIApp:
    """The full app; the outermost layers are the guard and the request size limit. restrict_peers
    accepts connections only from loopback and server.trusted_proxies."""
    mcp.settings.max_request_body_size = settings.max_request_bytes
    mcp.settings.max_sessions = MAX_SESSIONS
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(settings.hosts()),
        allowed_origins=sorted(settings.origins()),
    )
    mcp.streamable_http_app()  # creates mcp.session_manager with the settings above
    manager = mcp.session_manager
    resource = AnyHttpUrl(settings.resource_url)
    provider = Provider(store, settings) if settings.oauth.enabled else None
    metadata_url = build_resource_metadata_url(resource) if provider else None
    routes: list[Route] = [
        Route(HEALTH_PATH, health, methods=["GET", "HEAD"]),
        Route(MCP_PATH, RequireAuthMiddleware(StreamableHTTPASGIApp(manager), [], metadata_url)),
    ]
    if provider is not None:
        routes += _oauth_routes(provider, settings, resource)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    verifier = Verifier(store, settings, provider)
    app: ASGIApp = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                AuthenticationMiddleware, backend=BearerAuthBackend(verifier, resource_server_url=resource)
            ),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=lifespan,
    )
    if settings.cors_origins:
        app = CORSMiddleware(
            app,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Mcp-Session-Id",
                "Mcp-Protocol-Version",
                "Last-Event-ID",
            ],
            expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
        )
    app = RequestBodyLimitMiddleware(app, settings.max_request_bytes)

    async def valid(token: str) -> bool:
        return await verifier.verify_token(token) is not None

    return Guard(app, settings, restrict_peers=restrict_peers, verify=valid)


def uvicorn_config(app: ASGIApp, settings: RemoteSettings, **extra: Any) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.listen_host or "0.0.0.0",  # noqa: S104 (an empty host means every interface)
        port=settings.listen_port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=30,
        timeout_graceful_shutdown=5,
        **extra,
    )


def serve(mcp: FastMCP, settings: RemoteSettings, store: ServerStore, restrict_peers: bool = False) -> None:
    """Run until interrupted."""
    app = build_app(mcp, settings, store, restrict_peers)
    tokens = store.active_token_count()
    log.warning(
        "wa-mcp: MCP over HTTP at %s (listening on %s:%s); %d active token(s); OAuth %s",
        settings.resource_url,
        settings.listen_host or "0.0.0.0",  # noqa: S104
        settings.listen_port,
        tokens,
        f"on, approval by {settings.oauth.approval}" if settings.oauth.enabled else "off",
    )
    if settings.proxies() and not settings.trusted_proxies:
        log.info("wa-mcp: X-Forwarded-For from loopback is trusted (a TLS proxy on this host)")
    if tokens == 0 and not settings.oauth.enabled:
        log.warning(
            "wa-mcp: no client can connect yet: create a token with `wa-mcp token create --name NAME`"
        )
    uvicorn.Server(uvicorn_config(app, settings)).run()
