"""Verify signed bridge and agent requests."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_AGE_SECONDS = 300
SEEN_LIMIT = 10_000


@dataclass(frozen=True)
class Verdict:
    """The result of checking one delivery."""

    accepted: bool
    status: int  # the HTTP status to answer with
    reason: str
    duplicate: bool = False


@dataclass(frozen=True)
class Delivery:
    """A delivery that passed every check."""

    event: str
    delivery_id: str
    timestamp: int
    received_at: float
    payload: Mapping[str, Any]


class SeenDeliveries:
    """Delivery ids seen inside the replay window. The oldest are dropped first."""

    def __init__(self, window_seconds: float = MAX_AGE_SECONDS, limit: int = SEEN_LIMIT) -> None:
        self._window = window_seconds
        self._limit = limit
        self._items: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, delivery_id: str, now: float) -> bool:
        """Remember the id. Returns False when it was already seen."""
        with self._lock:
            while self._items and now - next(iter(self._items.values())) > self._window:
                self._items.popitem(last=False)
            if delivery_id in self._items:
                return False
            if len(self._items) >= self._limit:
                self._items.popitem(last=False)
            self._items[delivery_id] = now
            return True


def sign(secret: bytes, timestamp: str, delivery_id: str, body: bytes) -> str:
    """The signature header value the bridge sends: sha256=<hex of the HMAC>."""
    signed = f"{timestamp}.{delivery_id}.".encode() + body
    return "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()


def verify(
    secret: bytes,
    headers: Mapping[str, str],
    body: bytes,
    now: float,
    seen: SeenDeliveries,
    max_age: float = MAX_AGE_SECONDS,
) -> Verdict:
    """Check the headers of one delivery. Nothing is added to `seen` unless the signature is valid."""
    h = {k.lower(): v for k, v in headers.items()}
    stamp = h.get("x-wa-mcp-timestamp", "")
    delivery_id = h.get("x-wa-mcp-delivery", "")
    given = h.get("x-wa-mcp-signature", "")
    if not (re.fullmatch(r"[0-9]{1,19}", stamp) and delivery_id and given.isascii()):
        return Verdict(False, 401, "missing or malformed headers")
    if abs(now - int(stamp)) > max_age:
        return Verdict(False, 401, "timestamp outside the replay window")
    if not hmac.compare_digest(sign(secret, stamp, delivery_id, body), given):
        return Verdict(False, 401, "signature does not match")
    if not seen.add(delivery_id, now):
        return Verdict(True, 204, "delivery id seen before", duplicate=True)
    return Verdict(True, 204, "accepted")
