"""The parts of the MCP Python SDK that remote mode builds on, in one place.

Some of these live in SDK modules that are not part of its documented surface (the auth handlers, the
Streamable HTTP ASGI app, the body limit middleware). Every other module of this package imports them
from here, so an SDK upgrade (pinned to ~=1.30 in server/pyproject.toml) is checked in one file.
"""

from __future__ import annotations

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, build_resource_metadata_url
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken, ProtectedResourceMetadata

__all__ = [
    "AccessToken",
    "AuthContextMiddleware",
    "AuthenticationError",
    "AuthorizationCode",
    "AuthorizationHandler",
    "AuthorizationParams",
    "AuthorizeError",
    "BearerAuthBackend",
    "ClientAuthenticator",
    "ClientRegistrationOptions",
    "FastMCP",
    "MetadataHandler",
    "OAuthClientInformationFull",
    "OAuthToken",
    "ProtectedResourceMetadata",
    "ProtectedResourceMetadataHandler",
    "RefreshToken",
    "RegistrationError",
    "RegistrationHandler",
    "RequestBodyLimitMiddleware",
    "RequireAuthMiddleware",
    "RevocationOptions",
    "StreamableHTTPASGIApp",
    "TokenError",
    "TokenHandler",
    "TokenVerifier",
    "TransportSecuritySettings",
    "build_metadata",
    "build_resource_metadata_url",
    "construct_redirect_uri",
]
