"""OAuth 2.1 authorization server for remote MCP clients, on top of the MCP SDK handlers.

The SDK handlers parse and validate the requests (PKCE S256 only, exact redirect URI match, the code
and refresh grants, RFC 7591 registration, RFC 7009 revocation). This provider stores everything hashed
in server.db, requires a consent step for every authorization, spends codes once, rotates refresh tokens
and revokes the whole grant when a used code or refresh token comes back.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any
from urllib.parse import unquote_plus, urlencode, urlsplit

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message

from wa_mcp_server.common import blocking, clean_label
from wa_mcp_server.remote import store as store_mod
from wa_mcp_server.remote.sdk import (
    AccessToken,
    AuthenticationError,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    ClientAuthenticator,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    RegistrationError,
    TokenError,
    TokenHandler,
    TokenVerifier,
    construct_redirect_uri,
)
from wa_mcp_server.remote.settings import RemoteSettings, is_loopback
from wa_mcp_server.remote.store import Issued, RequestRecord, ServerStore

log = logging.getLogger("wa_mcp_server.remote")

SCOPE = "mcp"
REQUEST_SECONDS = 600
CODE_SECONDS = 120
MAX_REDIRECT_URIS = 5
MAX_URI_LENGTH = 512
# Schemes a native app may not use as a private-use redirect scheme (RFC 8252 section 7.1).
_BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss", "chrome"}
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# Client address of the registration request being handled (set by the /register route).
registration_ip: ContextVar[str] = ContextVar("registration_ip", default="")


def short_code() -> str:
    """Six characters shown on the consent page and next to the approval link, to match the two."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def check_redirect_uri(uri: str, prefixes: tuple[str, ...]) -> str | None:
    """Why a redirect URI is refused, or None. https; http only to a loopback host; or a private-use
    scheme of a native app. With prefixes configured, the URI must start with one of them."""
    if len(uri) > MAX_URI_LENGTH:
        return "redirect URI is too long"
    try:
        u = urlsplit(uri)
    except ValueError:
        return "redirect URI has an invalid address"
    scheme = u.scheme.lower()
    if u.fragment:
        return "redirect URI must not have a fragment"
    if u.username is not None or u.password is not None:
        return "redirect URI must not have user info"
    try:
        u.port  # noqa: B018 (raises ValueError for a port that is not a number in range)
    except ValueError:
        return "redirect URI has an invalid port"
    if scheme == "https":
        if not u.hostname:
            return "redirect URI needs a host"
    elif scheme == "http":
        if not is_loopback(u.hostname or ""):
            return "http redirect URIs are allowed only for a loopback address (use https)"
    elif not scheme or scheme in _BAD_SCHEMES:
        return f"redirect URI scheme {scheme!r} is not allowed"
    if prefixes and not any(uri.startswith(p) for p in prefixes):
        return (
            "redirect URI is not allowed by server.oauth.redirect_uri_prefixes (by default Claude and "
            "loopback addresses); the owner of this server can add it there"
        )
    return None


class GrantRefreshToken(RefreshToken):
    """A refresh token with the grant it belongs to (all tokens of one approval)."""

    grant_id: str


class Provider:
    """OAuthAuthorizationServerProvider backed by server.db."""

    def __init__(
        self,
        store: ServerStore,
        settings: RemoteSettings,
        announce: Callable[[RequestRecord, str, str], None] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.announce = announce

    # clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rec = await blocking(self.store.get_client, client_id)
        if rec is None or rec.revoked_at is not None:
            return None
        return OAuthClientInformationFull.model_validate({**rec.info, "client_id": rec.client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError(
                "invalid_redirect_uri", f"register 1 to {MAX_REDIRECT_URIS} redirect URIs"
            )
        for uri in uris:
            problem = check_redirect_uri(uri, self.settings.oauth.redirect_uri_prefixes)
            if problem:
                raise RegistrationError("invalid_redirect_uri", problem)
        if not set(client_info.grant_types) <= {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata", "only authorization_code and refresh_token")
        if client_info.token_endpoint_auth_method not in (
            "none",
            "client_secret_post",
            "client_secret_basic",
        ):
            raise RegistrationError("invalid_client_metadata", "unsupported token_endpoint_auth_method")
        if client_info.client_name:
            client_info.client_name = clean_label(client_info.client_name, 60)
        info = client_info.model_dump(mode="json", exclude_none=True, exclude={"client_secret", "client_id"})
        assert client_info.client_id is not None  # noqa: S101 (set by the SDK handler)
        ip = registration_ip.get()
        try:
            await blocking(
                self.store.add_client,
                client_info.client_id,
                info,
                client_info.client_secret,
                ip,
                self.settings.oauth.max_clients,
            )
        except ValueError as exc:
            raise RegistrationError("invalid_client_metadata", str(exc)) from exc
        log.warning(
            "oauth: client registered from %s: %s (%s)",
            ip or "unknown address",
            client_info.client_name or "unnamed",
            client_info.client_id,
        )

    async def client_record(self, client_id: str) -> store_mod.ClientRecord | None:
        rec = await blocking(self.store.get_client, client_id)
        return None if rec is None or rec.revoked_at is not None else rec

    # authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not params.redirect_uri_provided_explicitly:
            raise AuthorizeError("invalid_request", "redirect_uri is required")
        if params.resource and params.resource.rstrip("/") != self.settings.resource_url:
            raise AuthorizeError("invalid_request", f"unknown resource; use {self.settings.resource_url}")
        scopes = params.scopes or (client.scope.split() if client.scope else [SCOPE])
        assert client.client_id is not None  # noqa: S101
        request_id = secrets.token_urlsafe(24)
        ticket = secrets.token_urlsafe(32) if self.settings.oauth.approval == "link" else ""
        t = store_mod.now()
        rec = RequestRecord(
            id=request_id,
            code=short_code(),
            client_id=client.client_id,
            params={
                "state": params.state,
                "scopes": scopes,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "resource": self.settings.resource_url,
            },
            browser_hash=None,
            ticket_hash=self.store.hash(ticket) if ticket else None,
            status="pending",
            created_at=t,
            expires_at=t + REQUEST_SECONDS,
        )
        await blocking(self.store.add_request, rec)
        if ticket:
            link = self.approval_link(ticket)
            log.warning(
                "oauth: approval needed for %s (request %s); open within 10 minutes: %s",
                client.client_name or client.client_id,
                rec.code,
                link,
            )
            if self.announce:
                self.announce(rec, client.client_name or client.client_id, link)
        return f"{self.settings.base_url}/oauth/consent?request={request_id}"

    def approval_link(self, ticket: str) -> str:
        return f"{self.settings.base_url}/oauth/approve?ticket={ticket}"

    def new_ticket(self, rec: RequestRecord) -> str:
        """A fresh approval link for a pending request (wa-mcp oauth pending); the old one stops working."""
        ticket = secrets.token_urlsafe(32)
        self.store.set_ticket(rec.id, self.store.hash(ticket))
        return self.approval_link(ticket)

    def issue_code(self, rec: RequestRecord) -> str:
        """Create the authorization code of an approved request and return the redirect to the client."""
        code = secrets.token_urlsafe(32)
        p = rec.params
        self.store.add_code(
            self.store.hash(code),
            rec.client_id,
            {
                "scopes": p["scopes"],
                "code_challenge": p["code_challenge"],
                "redirect_uri": p["redirect_uri"],
                "resource": p["resource"],
            },
            store_mod.now() + CODE_SECONDS,
        )
        return construct_redirect_uri(p["redirect_uri"], code=code, state=p.get("state"))

    @staticmethod
    def denied_redirect(rec: RequestRecord) -> str:
        return construct_redirect_uri(
            rec.params["redirect_uri"],
            error="access_denied",
            error_description="the owner did not approve this client",
            state=rec.params.get("state"),
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        rec = await blocking(self.store.get_code, self.store.hash(authorization_code))
        if rec is None or rec.client_id != client.client_id:
            return None
        if rec.used_at is not None:
            if rec.grant_id:
                await blocking(self.store.revoke_grant, rec.grant_id)
            log.warning(
                "oauth: a used authorization code came back; its tokens are revoked (%s)", rec.client_id
            )
            return None
        d = rec.data
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(d["scopes"]),
            expires_at=float(rec.expires_at),
            client_id=rec.client_id,
            code_challenge=d["code_challenge"],
            redirect_uri=AnyUrl(d["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
            resource=d["resource"],
            subject="owner",
        )

    def _issued(self, grant_id: str) -> tuple[Issued, str, str]:
        access, refresh = (
            store_mod.new_secret(store_mod.ACCESS_PREFIX),
            store_mod.new_secret(store_mod.REFRESH_PREFIX),
        )
        t = store_mod.now()
        oauth = self.settings.oauth
        issued = Issued(
            grant_id=grant_id,
            access_hash=self.store.hash(access),
            access_expires=t + oauth.access_token_minutes * 60,
            refresh_hash=self.store.hash(refresh),
            refresh_expires=t + oauth.refresh_token_days * 86400,
        )
        return issued, access, refresh

    def _token(self, access: str, refresh: str, scopes: list[str]) -> OAuthToken:
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 (the token type, not a secret)
            expires_in=self.settings.oauth.access_token_minutes * 60,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        issued, access, refresh = self._issued("grant_" + secrets.token_hex(8))
        ok = await blocking(
            self.store.exchange_code,
            self.store.hash(authorization_code.code),
            authorization_code.client_id,
            authorization_code.scopes,
            authorization_code.resource or self.settings.resource_url,
            issued,
        )
        if not ok:
            raise TokenError("invalid_grant", "authorization code was already used or has expired")
        log.warning("oauth: access granted to %s", client.client_name or client.client_id)
        return self._token(access, refresh, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> GrantRefreshToken | None:
        rec = await blocking(self.store.get_token, self.store.hash(refresh_token))
        if rec is None or rec.kind != "refresh" or rec.client_id != client.client_id or rec.revoked:
            return None
        if rec.used_at is not None:
            await blocking(self.store.revoke_grant, rec.grant_id)
            log.warning(
                "oauth: a rotated refresh token was used again; the grant is revoked (%s)", rec.client_id
            )
            return None
        return GrantRefreshToken(
            token=refresh_token,
            client_id=rec.client_id,
            scopes=list(rec.scopes),
            expires_at=rec.expires_at,
            resource=rec.resource,
            subject="owner",
            grant_id=rec.grant_id,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: GrantRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        issued, access, refresh = self._issued(refresh_token.grant_id)
        ok = await blocking(
            self.store.rotate_refresh, self.store.hash(refresh_token.token), refresh_token.client_id, issued
        )
        if not ok:
            await blocking(self.store.revoke_grant, refresh_token.grant_id)
            raise TokenError("invalid_grant", "refresh token was already used")
        return self._token(access, refresh, refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(store_mod.ACCESS_PREFIX):
            return None
        rec = await blocking(self.store.get_token, self.store.hash(token))
        if rec is None or rec.kind != "access" or rec.revoked or rec.expires_at < store_mod.now():
            return None
        return AccessToken(
            token=token,
            client_id=rec.client_id,
            scopes=list(rec.scopes),
            expires_at=rec.expires_at,
            resource=rec.resource,
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        rec = await blocking(self.store.get_token, self.store.hash(token.token))
        if rec is not None:
            await blocking(self.store.revoke_grant, rec.grant_id)


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, secret = base64.b64decode(value, validate=True).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise AuthenticationError("Invalid Basic authentication header") from exc
    return unquote_plus(user), unquote_plus(secret)


async def token_endpoint(request: Request, handler: TokenHandler) -> Response:
    """Supply the authenticated client ID in the form required by the SDK token handler."""
    try:
        basic = _basic_credentials(request)
    except AuthenticationError:
        basic = None  # The authenticator returns the SDK's standard 401 response.
    if (
        basic
        and request.headers.get("content-type", "").split(";")[0].strip().lower()
        == "application/x-www-form-urlencoded"
    ):
        body = await request.body()
        if "client_id" not in await request.form():
            body += b"&" + urlencode({"client_id": basic[0]}).encode("ascii")
            headers = [(k, v) for k, v in request.scope["headers"] if k != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode("ascii")))

            async def receive() -> Message:
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request({**request.scope, "headers": headers}, receive)
    response: Response = await handler.handle(request)
    return response


class HashedClientAuthenticator(ClientAuthenticator):
    """Client authentication for /token and /revoke against the hashed client secrets in server.db."""

    def __init__(self, provider: Provider) -> None:
        super().__init__(provider)
        self.records = provider

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()
        basic = _basic_credentials(request)
        client_id = form.get("client_id", basic[0] if basic else None)
        if not isinstance(client_id, str) or not client_id:
            raise AuthenticationError("Missing client_id")
        rec = await self.records.client_record(client_id)
        if rec is None:
            raise AuthenticationError("Invalid client_id")
        method = rec.info.get("token_endpoint_auth_method") or "client_secret_post"
        secret: str | None = None
        if basic is not None:
            if method != "client_secret_basic" or basic[0] != client_id or "client_secret" in form:
                raise AuthenticationError("Client authentication method or ID mismatch")
            secret = basic[1]
        elif method == "client_secret_post":
            value = form.get("client_secret")
            secret = value if isinstance(value, str) else None
        if method != "none":
            expiry = rec.info.get("client_secret_expires_at")
            if expiry and expiry <= store_mod.now():
                raise AuthenticationError("Expired client_secret")
            if not (secret and rec.secret_hash and self.records.store.verify(secret, rec.secret_hash)):
                raise AuthenticationError("Invalid client_secret")
        return OAuthClientInformationFull.model_validate({**rec.info, "client_id": rec.client_id})


async def revoke_endpoint(request: Request, provider: Provider, authn: HashedClientAuthenticator) -> Response:
    """RFC 7009: revoke an access or refresh token of the calling client (its whole grant). Unknown
    tokens also answer 200. Public clients may leave out client_secret."""
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    try:
        client = await authn.authenticate_request(request)
    except AuthenticationError as exc:
        return JSONResponse({"error": "unauthorized_client", "error_description": exc.message}, 401, headers)
    token = (await request.form()).get("token")
    if not isinstance(token, str) or not token:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "token is required"}, 400, headers
        )
    rec = await blocking(provider.store.get_token, provider.store.hash(token))
    if rec is not None and rec.client_id == client.client_id:
        await blocking(provider.store.revoke_grant, rec.grant_id)
        log.warning("oauth: grant revoked by %s", client.client_name or client.client_id)
    return Response(status_code=200, headers=headers)


class Verifier(TokenVerifier):
    """Accepts static tokens (wam_) and, with OAuth on, OAuth access tokens (wao_)."""

    def __init__(self, store: ServerStore, settings: RemoteSettings, provider: Provider | None) -> None:
        self.store = store
        self.settings = settings
        self.provider = provider

    async def verify_token(self, token: str) -> AccessToken | None:
        token = token.strip()
        if token.startswith(store_mod.STATIC_PREFIX):
            info = await blocking(self.store.check_token, token)
            if info is None:
                return None
            return AccessToken(
                token=token,
                client_id=f"token:{info.id}",
                scopes=[SCOPE],
                resource=self.settings.resource_url,
                subject=info.name,
            )
        if self.provider is not None:
            return await self.provider.load_access_token(token)
        return None


def describe_client(info: dict[str, Any]) -> str:
    name = info.get("client_name") or "unnamed client"
    hosts = sorted(
        {urlsplit(str(u)).netloc or urlsplit(str(u)).scheme + ":" for u in info.get("redirect_uris", [])}
    )
    return f"{clean_label(str(name), 60)} ({', '.join(hosts)})"
