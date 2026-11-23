"""Outermost ASGI layer of remote mode: Host and Origin allow-lists, rate limits, security headers and a
short access log (method, path, status, client address; never query strings or headers)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wa_mcp_server.remote.settings import MCP_PATH, RemoteSettings, is_loopback

log = logging.getLogger("wa_mcp_server.remote.http")

AUTH_PATHS = ("/authorize", "/token", "/register", "/revoke")
HEALTH_PATH = "/health"
MAX_KEYS = 20000
# Scope key with the client address as the guard sees it (after trusted proxies).
CLIENT_IP_KEY = "wa_mcp.client_ip"
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)


@dataclass
class Buckets:
    """Token buckets per key: `per_minute` requests per `window` seconds (default a minute), refilled
    evenly. 0 means no limit."""

    per_minute: int
    clock: Callable[[], float] = time.monotonic
    window: float = 60.0
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)

    def _level(self, key: str, t: float) -> float:
        level, last = self._state.get(key, (float(self.per_minute), t))
        return min(float(self.per_minute), level + (t - last) * self.per_minute / self.window)

    def take(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        t = self.clock()
        level = self._level(key, t)
        if level < 1.0:
            self._state[key] = (level, t)
            return False
        if len(self._state) >= MAX_KEYS:
            self._prune(t)
        self._state[key] = (level - 1.0, t)
        return True

    def empty(self, key: str) -> bool:
        return self.per_minute > 0 and self._level(key, self.clock()) < 1.0

    def retry_after(self, key: str) -> int:
        if self.per_minute <= 0:
            return 0
        missing = 1.0 - self._level(key, self.clock())
        return max(1, math.ceil(missing * self.window / self.per_minute))

    def _prune(self, t: float) -> None:
        full = [k for k in self._state if self._level(k, t) >= self.per_minute]
        for k in full:
            del self._state[k]
        while len(self._state) >= MAX_KEYS:
            self._state.pop(next(iter(self._state)))


def client_ip(
    scope: Scope, headers: Headers, trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
) -> str:
    """The peer address; behind a trusted proxy the right-most untrusted X-Forwarded-For entry."""
    peer = scope.get("client")
    ip = str(peer[0]) if peer else "unknown"

    def is_trusted(value: str) -> bool:
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(addr in net for net in trusted)

    if not trusted or not is_trusted(ip):
        return ip
    hops = [h.strip() for h in ",".join(headers.getlist("x-forwarded-for")).split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            address = str(ipaddress.ip_address(hop))
        except ValueError:
            return ip
        if not is_trusted(address):
            return address
    return ip


def _bearer(headers: Headers) -> str:
    value = headers.get("authorization", "")
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


class Guard:
    """The address-based limits count only failed requests (4xx) of requests without a valid token, and
    they never refuse a request that carries a valid token; such a request counts against its token's own
    limit only. restrict_peers accepts connections only from loopback and the trusted proxies (a server
    behind a TLS proxy that listens on a non-loopback address)."""

    def __init__(
        self,
        app: ASGIApp,
        settings: RemoteSettings,
        clock: Callable[[], float] = time.monotonic,
        restrict_peers: bool = False,
        verify: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.app = app
        self.hosts = settings.hosts()
        self.origins = settings.origins()
        self.trusted = settings.proxies()
        self.restrict_peers = restrict_peers
        self.verify = verify
        self.failures = Buckets(settings.rate.per_ip_per_minute, clock)
        self.per_token = Buckets(settings.rate.per_token_per_minute, clock)
        self.auth = Buckets(settings.rate.auth_per_ip_per_minute, clock)
        self.register = Buckets(settings.rate.registrations_per_ip_per_hour, clock, window=3600.0)

    def _peer_allowed(self, scope: Scope) -> bool:
        peer = scope.get("client")
        host = str(peer[0]) if peer else ""
        if is_loopback(host):
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(addr in net for net in self.trusted)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = Headers(scope=scope)
        path: str = scope.get("path", "")
        method: str = scope.get("method", "")
        shown = log_path(scope)
        ip = client_ip(scope, headers, self.trusted)
        scope[CLIENT_IP_KEY] = ip
        refused = self._access(scope, headers, path)
        token = _bearer(headers) if path == MCP_PATH and refused is None else ""
        valid = bool(token) and self.verify is not None and await self.verify(token)
        if path != HEALTH_PATH:
            if not valid and self.failures.empty(ip):
                refused = 429, "too many failed requests from this address", self.failures.retry_after(ip)
            elif refused is None:
                refused = self._rate(path, method, ip, token if valid else "")
        if refused is not None:
            status, message, retry = refused
            await _reply(send, status, message, retry)
            if status != 429 and not valid:
                self.failures.take(ip)
            log.info("http %s %s %d %s", method, shown, status, ip)
            return

        status_seen = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_seen
            if message["type"] == "http.response.start":
                status_seen = int(message["status"])
                raw = list(message.get("headers", []))
                names = {k.lower() for k, _ in raw}
                raw += [(k, v) for k, v in _SECURITY_HEADERS if k not in names]
                if b"cache-control" not in names:
                    raw.append((b"cache-control", b"no-store"))
                message = {**message, "headers": raw}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not valid and 400 <= status_seen < 500 and path != HEALTH_PATH:
                self.failures.take(ip)
                if path == MCP_PATH or _sensitive(path, method):
                    self.auth.take(ip)
            log.info("http %s %s %d %s", method, shown, status_seen, ip)

    def _access(self, scope: Scope, headers: Headers, path: str) -> tuple[int, str, int] | None:
        if path == HEALTH_PATH:
            return None
        if self.restrict_peers and not self._peer_allowed(scope):
            return 403, "connect through the TLS proxy", 0
        host = headers.get("host", "").lower()
        if host not in self.hosts:
            return 421, "host not allowed", 0
        origin = headers.get("origin")
        if origin is not None and origin.lower().rstrip("/") not in self.origins:
            return 403, "origin not allowed", 0
        return None

    def _rate(self, path: str, method: str, ip: str, valid_token: str) -> tuple[int, str, int] | None:
        if valid_token:
            key = hashlib.sha256(valid_token.encode()).hexdigest()
            if not self.per_token.take(key):
                return 429, "too many requests for this token", self.per_token.retry_after(key)
            return None
        if (path == MCP_PATH or _sensitive(path, method)) and self.auth.empty(ip):
            return 429, "too many failed authorization attempts", self.auth.retry_after(ip)
        if path == "/register" and method == "POST" and not self.register.take(ip):
            return 429, "too many client registrations from this address", self.register.retry_after(ip)
        return None


def _sensitive(path: str, method: str) -> bool:
    """OAuth endpoints and consent form posts (the consent page reloads, GET, are not)."""
    return path in AUTH_PATHS or (path.startswith("/oauth/") and method == "POST")


_LOG_UNSAFE = re.compile(r"[\x00-\x20\x7f-\xff]")


def log_path(scope: Scope) -> str:
    """The request path as sent (still percent-encoded), with control and non-ASCII bytes escaped, so a
    request cannot add lines or fake entries to the access log."""
    raw = scope.get("raw_path") or str(scope.get("path", "")).encode("utf-8", "replace")
    text = bytes(raw).decode("latin-1")
    text = _LOG_UNSAFE.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    return text[:300]


async def _reply(send: Send, status: int, message: str, retry: int) -> None:
    body = json.dumps({"ok": False, "error": message}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        *_SECURITY_HEADERS,
    ]
    if retry:
        headers.append((b"retry-after", str(retry).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
