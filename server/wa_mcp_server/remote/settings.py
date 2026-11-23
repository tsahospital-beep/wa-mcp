"""The "server" section of config.json (read by the Python side only) and the HTTPS start guard."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from wa_mcp import config as config_mod

ConfigError = config_mod.ConfigError

APPROVAL_MODES = ("link", "password")
MCP_PATH = "/mcp"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class TransportRefused(ValueError):
    """Remote mode would expose plain HTTP without an explicit choice."""


@dataclass(frozen=True)
class RateLimits:
    per_token_per_minute: int
    per_ip_per_minute: int
    auth_per_ip_per_minute: int
    registrations_per_ip_per_hour: int


@dataclass(frozen=True)
class OAuthSettings:
    enabled: bool
    approval: str
    admin_password_env: str
    access_token_minutes: int
    refresh_token_days: int
    max_clients: int
    redirect_uri_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class RemoteSettings:
    listen_host: str
    listen_port: int
    public_url: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    cors_origins: tuple[str, ...]
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    max_request_bytes: int
    rate: RateLimits
    oauth: OAuthSettings
    store_dir: Path

    @property
    def base_url(self) -> str:
        """Public address without a trailing slash: public_url, else the loopback listen address."""
        if self.public_url:
            return self.public_url
        host = self.listen_host
        if is_unspecified(host) or host == "localhost":
            host = "127.0.0.1"
        return f"http://{bracket(host)}:{self.listen_port}"

    @property
    def resource_url(self) -> str:
        return self.base_url + MCP_PATH

    @property
    def server_db(self) -> Path:
        return self.store_dir / "server.db"

    def proxies(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """Peers whose X-Forwarded-For is trusted: server.trusted_proxies, or loopback when the server
        listens on loopback behind an https public_url (a proxy on the same host)."""
        if self.trusted_proxies:
            return self.trusted_proxies
        if is_loopback(self.listen_host) and self.public_url.startswith("https://"):
            return (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
        return ()

    def hosts(self) -> set[str]:
        """Host header values accepted. Loopback names with the listen port always work (a DNS
        rebinding page cannot send them), plus the listen address and the public host."""
        port = self.listen_port
        out = {f"{h}:{port}" for h in LOOPBACK_NAMES}
        if not is_unspecified(self.listen_host):
            out.add(f"{bracket(self.listen_host)}:{port}".lower())
        if self.public_url:
            u = urlsplit(self.public_url)
            out.add(u.netloc.lower())
            if u.port is None:
                default = 443 if u.scheme == "https" else 80
                out.add(f"{u.netloc.lower()}:{default}")
        out.update(h.lower() for h in self.allowed_hosts)
        return out

    def origins(self) -> set[str]:
        """Origin header values accepted. Requests without Origin (non-browser clients) pass."""
        out = {origin_of(self.base_url)}
        if is_loopback(urlsplit(self.base_url).hostname or ""):
            out.update(f"http://{h}:{self.listen_port}" for h in LOOPBACK_NAMES)
        out.update(o.lower().rstrip("/") for o in (*self.allowed_origins, *self.cors_origins))
        return out


def bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_unspecified(host: str) -> bool:
    host = host.strip("[]")
    if host == "":
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def origin_of(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}".lower()


def split_listen(value: str, where: str = "server.listen") -> tuple[str, int]:
    host, sep, port = value.strip().rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"{where}: want host:port, got {value!r}")
    return host.strip("[]"), int(port)


secret_env = config_mod.secret_env


def _list(node: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = node.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{where}.{key}: must be a list of strings")
    return tuple(x.strip() for x in value if x.strip())


def _int(node: dict[str, Any], key: str, where: str, low: int, high: int) -> int:
    value = node.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or (isinstance(value, float) and not value.is_integer())
    ):
        raise ConfigError(f"{where}.{key}: must be a whole number")
    if not low <= value <= high:
        raise ConfigError(f"{where}.{key}: must be between {low} and {high}")
    return int(value)


def _bool(node: dict[str, Any], key: str, where: str) -> bool:
    value = node.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: must be true or false")
    return value


def _str(node: dict[str, Any], key: str, where: str) -> str:
    value = node.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: must be a string")
    return value.strip()


def _obj(node: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = node.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.{key}: must be an object")
    return value


def _prefix(value: str) -> str:
    """A redirect prefix. A web address without a path gets "/" so that https://app.example.test cannot
    match https://app.example.test.evil.test; a prefix that ends with ":" stands for any port."""
    try:
        u = urlsplit(value)
        _ = u.port  # Validate the port when present.
    except ValueError as exc:
        raise ConfigError(f"server.oauth.redirect_uri_prefixes: invalid address {value!r}") from exc
    if not u.scheme:
        raise ConfigError(f"server.oauth.redirect_uri_prefixes: {value!r} needs a scheme")
    if u.scheme in ("http", "https"):
        if not u.hostname or u.username is not None or u.fragment:
            raise ConfigError(f"server.oauth.redirect_uri_prefixes: invalid web address {value!r}")
        if not u.path and not u.query and not value.endswith(":"):
            return value + "/"
    return value


def _public_url(value: str) -> str:
    if not value:
        return ""
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.query or u.fragment:
        raise ConfigError("server.public_url: need http(s)://host[:port] without user, query or fragment")
    if u.path not in ("", "/"):
        raise ConfigError("server.public_url: serve the MCP server at the root of its host (no path)")
    return f"{u.scheme}://{u.netloc}".lower()


def _origin(value: str, where: str) -> str:
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query:
        raise ConfigError(f"{where}: {value!r} is not an origin such as https://app.example.com")
    return f"{u.scheme}://{u.netloc}".lower()


def _networks(values: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    out = []
    for v in values:
        try:
            out.append(ipaddress.ip_network(v, strict=False))
        except ValueError as exc:
            raise ConfigError(f"server.trusted_proxies: {v!r} is not an address or network") from exc
    return tuple(out)


def load(cfg: config_mod.Config, listen: str | None = None) -> RemoteSettings:
    """Read the server section; listen overrides server.listen (the --listen flag)."""
    node = cfg.section("server")
    where = "server"
    host, port = split_listen(listen or _str(node, "listen", where) or "127.0.0.1:8765")
    rate = _obj(node, "rate_limit", where)
    oauth = _obj(node, "oauth", where)
    approval = _str(oauth, "approval", "server.oauth") or "link"
    if approval not in APPROVAL_MODES:
        raise ConfigError(f"server.oauth.approval: use one of {', '.join(APPROVAL_MODES)}")
    prefixes = tuple(_prefix(p) for p in _list(oauth, "redirect_uri_prefixes", "server.oauth"))
    return RemoteSettings(
        listen_host=host,
        listen_port=port,
        public_url=_public_url(_str(node, "public_url", where)),
        allowed_hosts=_list(node, "allowed_hosts", where),
        allowed_origins=tuple(
            _origin(o, "server.allowed_origins") for o in _list(node, "allowed_origins", where)
        ),
        cors_origins=tuple(_origin(o, "server.cors_origins") for o in _list(node, "cors_origins", where)),
        trusted_proxies=_networks(_list(node, "trusted_proxies", where)),
        max_request_bytes=_int(node, "max_request_kb", where, 16, 16384) * 1024,
        rate=RateLimits(
            per_token_per_minute=_int(rate, "per_token_per_minute", "server.rate_limit", 0, 100000),
            per_ip_per_minute=_int(rate, "per_ip_per_minute", "server.rate_limit", 0, 100000),
            auth_per_ip_per_minute=_int(rate, "auth_per_ip_per_minute", "server.rate_limit", 0, 100000),
            registrations_per_ip_per_hour=_int(
                rate, "registrations_per_ip_per_hour", "server.rate_limit", 0, 100000
            ),
        ),
        oauth=OAuthSettings(
            enabled=_bool(oauth, "enabled", "server.oauth"),
            approval=approval,
            admin_password_env=_str(oauth, "admin_password_env", "server.oauth"),
            access_token_minutes=_int(oauth, "access_token_minutes", "server.oauth", 1, 1440),
            refresh_token_days=_int(oauth, "refresh_token_days", "server.oauth", 1, 365),
            max_clients=_int(oauth, "max_clients", "server.oauth", 1, 1000),
            redirect_uri_prefixes=prefixes,
        ),
        store_dir=cfg.store_dir,
    )


def restrict_peers(s: RemoteSettings, insecure_lan: bool) -> bool:
    """Behind a TLS proxy on a non-loopback address, only the proxy (and loopback) may connect."""
    return bool(s.public_url.startswith("https://") and not is_loopback(s.listen_host) and not insecure_lan)


def check_transport(s: RemoteSettings, insecure_lan: bool) -> str:
    """Refuse plain HTTP that other machines can reach. Returns a warning ("" when there is none).

    Allowed: a loopback listen address; an https server.public_url (a TLS proxy in front), which on a
    non-loopback address also needs server.trusted_proxies; anything else only with --insecure-lan.
    """
    loopback = is_loopback(s.listen_host)
    public = urlsplit(s.public_url) if s.public_url else None
    https_proxy = public is not None and public.scheme == "https"
    local_public = public is None or (public.scheme == "http" and is_loopback(public.hostname or ""))
    if https_proxy and not loopback and not insecure_lan:
        if not s.trusted_proxies:
            raise TransportRefused(
                f"server.public_url is https and the server listens on {bracket(s.listen_host)}:{s.listen_port}: "
                "set server.trusted_proxies to the address of the TLS proxy, so that nobody can bypass it, "
                "or listen on a loopback address"
            )
        warning = (
            f"listening on {bracket(s.listen_host)}:{s.listen_port} behind the TLS proxy {s.public_url}: "
            "connections are accepted only from loopback and server.trusted_proxies"
        )
    elif https_proxy or (loopback and local_public):
        warning = ""
    elif insecure_lan:
        warning = (
            f"--insecure-lan: MCP over plain HTTP on {bracket(s.listen_host)}:{s.listen_port}. Tokens and "
            "messages cross the network unencrypted. Use it only on a network you trust, or put a TLS proxy "
            "in front and set server.public_url to its https address."
        )
    elif public is not None and public.scheme == "http":
        raise TransportRefused(
            "server.public_url must be https: put a TLS proxy (for example Caddy) in front of the server, "
            "or pass --insecure-lan to accept plain HTTP"
        )
    else:
        raise TransportRefused(
            f"refusing to serve plain HTTP on {bracket(s.listen_host)}:{s.listen_port}: listen on a loopback "
            "address, set server.public_url to the https address of a TLS proxy, or pass --insecure-lan"
        )
    if s.oauth.enabled and not (https_proxy or is_loopback(urlsplit(s.base_url).hostname or "")):
        raise TransportRefused("server.oauth needs an https server.public_url (or a loopback address)")
    return warning
