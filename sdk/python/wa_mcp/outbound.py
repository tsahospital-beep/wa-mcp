"""Rules for requests to services the user configures (AI provider, HTTP transcription): https unless the
service runs on a loopback address, and answers read with a size limit."""

from __future__ import annotations

import ipaddress
import time
import zlib
from typing import Any

import httpx


class BodyError(Exception):
    """The service returned an unreadable response body."""


class TooLarge(BodyError):
    """The answer is larger than the limit; nothing of it is used."""


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def insecure(url: str) -> bool:
    """True for plain http to another machine (the only allowed schemes are https and loopback http)."""
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return True
    return not target.host or (
        target.scheme != "https" and not (target.scheme == "http" and is_loopback(target.host))
    )


def read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Body of resp, decompressed here so the cap applies to the decompressed size."""
    if resp.is_stream_consumed:
        # Already read by httpx (in-memory responses); only the size check is left.
        if len(resp.content) > max_bytes:
            raise TooLarge(f"response is larger than {max_bytes} bytes")
        return resp.content
    encoding = resp.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "x-gzip", "deflate"):
        raise BodyError(f"unsupported content encoding {encoding}")
    inflate = zlib.decompressobj(47) if encoding not in ("", "identity") else None
    body = bytearray()
    # Compressed streams can carry headers and empty members without producing any output.
    wire_remaining = max_bytes + 64 * 1024 if inflate is not None else max_bytes
    deadline = resp.extensions.get("read_deadline")
    for chunk in resp.iter_raw():
        if deadline is not None and time.monotonic() >= deadline:
            raise httpx.ReadTimeout("response deadline reached")
        wire_remaining -= len(chunk)
        if wire_remaining < 0:
            raise TooLarge(f"response is larger than the wire limit for {max_bytes} bytes")
        if inflate is None:
            body.extend(chunk)
        else:
            remaining = chunk
            while remaining:
                if inflate.eof:
                    if encoding not in ("gzip", "x-gzip"):
                        raise BodyError("trailing bytes in deflate body")
                    inflate = zlib.decompressobj(31)
                try:
                    body.extend(inflate.decompress(remaining, max_bytes + 1 - len(body)))
                except zlib.error as exc:
                    raise BodyError("the compressed body is damaged") from exc
                if len(body) > max_bytes or inflate.unconsumed_tail:
                    raise TooLarge(f"response is larger than {max_bytes} bytes")
                remaining = inflate.unused_data
        if len(body) > max_bytes:
            raise TooLarge(f"response is larger than {max_bytes} bytes")
    if inflate is not None and not inflate.eof:
        raise BodyError("incomplete compressed body")
    return bytes(body)


def post(client: httpx.Client, url: str, limit: int, **kwargs: Any) -> httpx.Response:
    """Read a bounded response and keep its original content type."""
    headers = {"Accept-Encoding": "gzip, deflate", **kwargs.pop("headers", {})}
    with client.stream("POST", url, headers=headers, **kwargs) as stream:
        body = read_capped(stream, limit)
        headers = dict(stream.headers)
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(stream.status_code, headers=headers, content=body, request=stream.request)
