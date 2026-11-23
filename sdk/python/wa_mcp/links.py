"""Fetch public Google Docs/Sheets/Slides shared in chats, and check link status.

Every request goes through one guard: https only, no credentials in the URL,
default port only, every resolved address must be public, redirects are
re-checked and may not leave the allowed hosts, bodies are size-capped and
requests time out. The connection goes to the address that was checked (TLS
SNI and the Host header keep the name), so a DNS answer cannot change between
check and connect. Addresses that embed a private IPv4 address (IPv4-mapped,
IPv4-compatible, NAT64, 6to4, Teredo) are refused. Compressed bodies are
decompressed with the same size cap. Google exports start at docs.google.com and
may only be redirected to a *.googleusercontent.com download host.

Fetching is a request from the user's own computer: the remote site (Google
for shared docs) sees the user's IP address.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from wa_mcp import db, outbound, people

DOC_HOSTS = frozenset({"docs.google.com"})
# Google serves export downloads from these hosts after a redirect.
DOC_REDIRECT_SUFFIXES = (".googleusercontent.com",)
MAX_BYTES = 5 * 1024 * 1024
_EMBEDS_V4 = (
    ipaddress.ip_network("::/96"),  # IPv4-compatible (deprecated), also :: and ::1
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
)
TIMEOUT = 20.0
MAX_REDIRECTS = 3

_DOC_RE = re.compile(
    r"https://docs\.google\.com/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{10,})(?:/[^\s#?]*)?"
    r"(?:\?[^\s#]*)?(?:#gid=(\d+))?"
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

Resolver = Callable[[str], list[str]]


class LinkError(Exception):
    """The URL was refused by the guard or the fetch failed."""


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LinkError(f"cannot resolve {host}") from exc
    return sorted({str(info[4][0]) for info in infos})


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _blocked(ip.ipv4_mapped)
        if any(ip in net for net in _EMBEDS_V4) or ip.teredo is not None:
            return True
        if ip.sixtofour is not None and _blocked(ip.sixtofour):
            return True
    return not ip.is_global or ip.is_multicast


def _host_allowed(host: str, hosts: frozenset[str] | None, suffixes: tuple[str, ...]) -> bool:
    if hosts is None:
        return True
    return host in hosts or any(host.endswith(suffix) and host != suffix.lstrip(".") for suffix in suffixes)


def check_url(
    url: str,
    allowed_hosts: frozenset[str] | None = None,
    resolve: Resolver = _resolve,
    allowed_suffixes: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Raise LinkError unless url is safe to fetch; returns (host, address to connect to)."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise LinkError("only https links are fetched")
    if parts.username is not None or parts.password is not None:
        raise LinkError("links with credentials are refused")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise LinkError("link has no host")
    try:
        port = parts.port
    except ValueError as exc:  # for example :99999 or :abc
        raise LinkError("link has an invalid port") from exc
    if port not in (None, 443):
        raise LinkError("only the default https port is allowed")
    if not _host_allowed(host, allowed_hosts, allowed_suffixes):
        raise LinkError(f"host {host} is not allowed")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal else resolve(host)
    if not addresses:
        raise LinkError(f"cannot resolve {host}")
    for addr in addresses:
        if _blocked(ipaddress.ip_address(addr)):
            raise LinkError(f"{host} points to a private or reserved address")
    return host, addresses[0]


def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    try:
        return outbound.read_capped(resp, max_bytes)
    except outbound.BodyError as exc:
        raise LinkError(str(exc)) from exc


def _pinned(url: str, host: str, address: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """URL aimed at the checked address, plus the Host header and SNI name that keep the real host."""
    parts = urlsplit(url)
    ip = ipaddress.ip_address(address)
    netloc = f"[{ip}]" if ip.version == 6 else str(ip)
    target = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    if host.strip("[]") == str(ip):
        return target, {}, {}
    return target, {"Host": host}, {"sni_hostname": host}


@dataclass
class Fetched:
    url: str
    status: int
    content_type: str
    body: bytes


def guarded_get(
    url: str,
    allowed_hosts: frozenset[str] | None = None,
    max_bytes: int = MAX_BYTES,
    method: str = "GET",
    resolve: Resolver = _resolve,
    transport: httpx.BaseTransport | None = None,
    read_body: bool = True,
    redirect_suffixes: tuple[str, ...] = (),
) -> Fetched:
    """GET (or HEAD) with the guard applied to the URL and every redirect.

    redirect_suffixes: extra host suffixes allowed only for redirect targets, never for the first URL.
    """
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        # The URL used by the transport contains the pinned IP. A new client
        # keeps TLS sessions and cookies specific to the actual redirect host.
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=False, transport=transport, trust_env=False,
            headers={"User-Agent": "wa-mcp-link-check", "Accept-Encoding": "gzip, deflate"},
        ) as client:  # fmt: skip
            host, address = check_url(current, allowed_hosts, resolve, redirect_suffixes if hop else ())
            target, headers, extensions = _pinned(current, host, address)
            try:
                with client.stream(method, target, headers=headers, extensions=extensions) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location", "")
                        if not location:
                            raise LinkError("redirect without a location")
                        current = urljoin(current, location)
                        continue
                    body = b""
                    if read_body:
                        declared = resp.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > max_bytes:
                            raise LinkError(f"response is larger than {max_bytes} bytes")
                        body = _read_capped(resp, max_bytes)
                    return Fetched(current, resp.status_code, resp.headers.get("content-type", ""), body)
            except httpx.TimeoutException as exc:
                raise LinkError("the request timed out") from exc
            except httpx.HTTPError as exc:
                raise LinkError(f"request failed: {exc.__class__.__name__}") from exc
    raise LinkError("too many redirects")


@dataclass(frozen=True)
class DocLink:
    kind: str  # document | spreadsheets | presentation
    doc_id: str
    gid: str | None
    url: str

    @property
    def export_url(self) -> str:
        base = f"https://docs.google.com/{self.kind}/d/{self.doc_id}"
        if self.kind == "document":
            return f"{base}/export?format=txt"
        if self.kind == "spreadsheets":
            return f"{base}/export?format=csv" + (f"&gid={self.gid}" if self.gid else "")
        return f"{base}/export/txt"


def find_doc_links(text: str) -> list[DocLink]:
    out: list[DocLink] = []
    for m in _DOC_RE.finditer(text or ""):
        link = DocLink(m.group(1), m.group(2), m.group(3), m.group(0))
        if all(x.doc_id != link.doc_id for x in out):
            out.append(link)
    return out


def find_urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL_RE.findall(text or "")))


def fetch_doc(
    link: DocLink, transport: httpx.BaseTransport | None = None, resolve: Resolver = _resolve
) -> str:
    got = guarded_get(
        link.export_url,
        allowed_hosts=DOC_HOSTS,
        transport=transport,
        resolve=resolve,
        redirect_suffixes=DOC_REDIRECT_SUFFIXES,
    )
    if got.status != 200:
        raise LinkError(f"export answered {got.status}; the document may not be public")
    if "text/html" in got.content_type:
        raise LinkError("got a sign-in page; the document is not shared publicly")
    return got.body.decode("utf-8-sig", "replace").strip()


def fetch_shared_docs(
    chat: str,
    after: db.TimeArg = None,
    before: db.TimeArg = None,
    limit: int = 20,
    transport: httpx.BaseTransport | None = None,
    resolve: Resolver = _resolve,
) -> dict[str, Any]:
    """Download public Google docs linked in a chat; text stored as derived_text source 'link'."""
    rows = db.messages(
        conv=people.same_person(chat), after=after, before=before, query="docs.google.com", limit=500
    )
    fetched: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in rows:
        for link in find_doc_links(str(row["body"])):
            if len(fetched) + len(failed) >= limit:
                break
            try:
                if db.store().cfg.demo:
                    raise LinkError("Demo mode: external documents are offline.")
                text = fetch_doc(link, transport=transport, resolve=resolve)
            except LinkError as exc:
                failed.append({"msg_id": row["msg_id"], "url": link.url, "error": str(exc)})
                continue
            db.put_derived(str(row["conv"]), str(row["msg_id"]), "link", f"{link.url}\n\n{text}")
            fetched.append({"msg_id": row["msg_id"], "url": link.url, "kind": link.kind, "chars": len(text)})
    return {"fetched": fetched, "failed": failed}


def link_status(
    url: str, transport: httpx.BaseTransport | None = None, resolve: Resolver = _resolve
) -> dict[str, Any]:
    """HEAD (then a capped GET) through the guard; never raises."""
    try:
        got = guarded_get(url, method="HEAD", transport=transport, resolve=resolve, read_body=False)
        if got.status in (405, 501):
            got = guarded_get(url, transport=transport, resolve=resolve, read_body=False)
    except (LinkError, ValueError) as exc:
        return {"url": url, "ok": False, "error": str(exc)}
    return {"url": url, "ok": got.status < 400, "status": got.status, "final_url": got.url}


def check_chat_links(
    chat: str,
    after: db.TimeArg = None,
    before: db.TimeArg = None,
    limit: int = 20,
    transport: httpx.BaseTransport | None = None,
    resolve: Resolver = _resolve,
) -> dict[str, Any]:
    """Status of the https links found in a chat (newest first), checked through the same guard."""
    rows = db.messages(conv=people.same_person(chat), after=after, before=before, query="https://", limit=500)
    seen: dict[str, str] = {}
    for row in rows:
        for url in find_urls(str(row["body"])):
            if url not in seen and len(seen) < limit:
                seen[url] = str(row["msg_id"])
    checked = []
    for url, mid in seen.items():
        status = (
            {"url": url, "ok": False, "error": "Demo mode: external links are offline."}
            if db.store().cfg.demo
            else link_status(url, transport=transport, resolve=resolve)
        )
        checked.append({**status, "msg_id": mid})
    return {"links": checked, "count": len(checked)}
