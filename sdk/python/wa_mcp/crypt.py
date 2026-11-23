"""Field sealing compatible with the bridge (bridge/internal/crypt).

Sealed value: "wae1:" + base64(nonce(12) || AES-256-GCM ciphertext and tag), or "wae2:" with the same
layout and associated data that names the place of the value: table|column|conv|msg_id (see aad), so a
value copied to another row or column does not open. New values are "wae2:".
Key: Argon2id(passphrase, salt, time=3, memory=64 MiB, threads=2, length=32), or for a
random 32-byte store key (OS keystore or key file) HKDF-SHA256(store key, salt, FIELD_KEY_INFO).
Needs the optional extra "encryption" (see wa_mcp.install_hint).
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import os
from typing import Any

from wa_mcp import install_hint

PREFIX = "wae1:"
PREFIX_V2 = "wae2:"
CHECK_PLAINTEXT = "wa-mcp"
INSTALL_HINT = install_hint("encryption")

_ARGON_TIME = 3
_ARGON_MEMORY_KIB = 64 * 1024
_ARGON_THREADS = 2
_KEY_LEN = 32
_NONCE_LEN = 12
_TAG_LEN = 16


class CryptError(Exception):
    """A sealed value cannot be opened or the crypto packages are missing."""


def is_sealed(value: str | bytes | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bytes):
        return value.startswith((PREFIX.encode(), PREFIX_V2.encode()))
    return value.startswith((PREFIX, PREFIX_V2))


def aad(table: str, column: str, conv: str, msg_id: str = "", source: str = "") -> bytes:
    """Associated data of a "wae2:" value, as crypt.AAD in Go (testdata/crypt_vectors_v2.json): the UTF-8 of
    table|column|conv|msg_id, plus |source for derived_text. Rows without a message use their own key for
    conv (scheduled_messages: target, wa_contacts: jid) and "" for msg_id."""
    parts = [table, column, conv, msg_id, *([source] if source else [])]
    return "|".join(parts).encode()


@functools.lru_cache(maxsize=4)
def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Argon2id key; cached because each call costs 64 MiB and noticeable time."""
    try:
        from argon2.low_level import Type, hash_secret_raw
    except ImportError as exc:
        raise CryptError(f"encryption support is not installed: {INSTALL_HINT}") from exc
    key: bytes = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON_TIME,
        memory_cost=_ARGON_MEMORY_KIB,
        parallelism=_ARGON_THREADS,
        hash_len=_KEY_LEN,
        type=Type.ID,
    )
    return key


FIELD_KEY_INFO = b"wa-mcp/wa.db/fields/v1"
STORE_KEY_LEN = 32


def field_key(store_key: bytes, salt: bytes) -> bytes:
    """HKDF-SHA256 (RFC 5869, one output block) of the store key, as crypt.FieldKey in Go."""
    if len(store_key) != STORE_KEY_LEN:
        raise CryptError("store key must be 32 bytes")
    prk = hmac.new(salt, store_key, hashlib.sha256).digest()
    return hmac.new(prk, FIELD_KEY_INFO + b"\x01", hashlib.sha256).digest()


class Sealer:
    """Seals and opens field values with one key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LEN:
            raise CryptError("key must be 32 bytes")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise CryptError(f"encryption support is not installed: {INSTALL_HINT}") from exc
        self._aead: Any = AESGCM(key)

    def seal(self, plain: bytes, associated: bytes | None = None) -> str:
        """A "wae2:" value bound to associated data, or "wae1:" without it (only meta.crypt_check)."""
        nonce = os.urandom(_NONCE_LEN)
        out = nonce + self._aead.encrypt(nonce, plain, associated)
        return (PREFIX_V2 if associated is not None else PREFIX) + base64.b64encode(out).decode("ascii")

    def open(self, value: str, associated: bytes | None = None) -> bytes:
        """Open a "wae2:" value with its associated data, or a "wae1:" value (which has none)."""
        if value.startswith(PREFIX_V2):
            if associated is None:
                raise CryptError("this sealed value needs its place (associated data) to open")
        elif value.startswith(PREFIX):
            associated = None
        else:
            raise CryptError("value is not sealed")
        try:
            raw = base64.b64decode(value[len(PREFIX) :], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CryptError("sealed value is damaged or the key is wrong") from exc
        if len(raw) < _NONCE_LEN + _TAG_LEN:
            raise CryptError("sealed value is too short")
        try:
            plain: bytes = self._aead.decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], associated)
        except Exception as exc:
            raise CryptError("sealed value is damaged or the key is wrong") from exc
        return plain

    def seal_string(self, plain: str, associated: bytes | None = None) -> str:
        return self.seal(plain.encode("utf-8"), associated) if plain else ""

    def open_string(self, value: str, associated: bytes | None = None) -> str:
        """Values without the prefix pass through (rows written before encryption)."""
        if not is_sealed(value):
            return value
        return self.open(value, associated).decode("utf-8")

    def open_blob(self, value: bytes) -> bytes:
        if not is_sealed(value):
            return value
        return self.open(value.decode("ascii"))

    def verify_check(self, check: str) -> bool:
        try:
            return self.open(check) == CHECK_PLAINTEXT.encode()
        except CryptError:
            return False
