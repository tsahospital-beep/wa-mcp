"""Secrets of one store folder in the OS keystore, read the same way as the bridge
(bridge/internal/keystore).

- Windows: DPAPI in user scope. store/key.dpapi holds the store key and store/api.token.dpapi
  the API token; a copy opened by another Windows user or on another computer cannot be unwrapped.
- macOS: the login Keychain through the ``security`` tool (service "wa-mcp").
- Linux and others: the Secret Service through ``secret-tool`` (attributes service and account).

Keychain and Secret Service items are named ``<item>.<id>``, where the id comes from
store/keystore.id, so a moved store folder keeps its items. Secrets are never passed as
program arguments and never logged. Key files (encryption.mode keyfile) are read here too.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from wa_mcp import secretfile

ITEM_STORE_KEY = "store-key"
ITEM_API_TOKEN = "api-token"  # noqa: S105 (an item name, not a secret)
SERVICE = "wa-mcp"
ID_FILE = "keystore.id"
STORE_KEY_LEN = 32
_TIMEOUT = 15
_ID = re.compile(r"^[0-9a-f]{32}$")


class KeystoreError(Exception):
    """A keystore item or key file exists but cannot be read."""


class Unavailable(KeystoreError):
    """There is no usable OS keystore on this system."""


def dpapi_file(item: str) -> str:
    return "key.dpapi" if item == ITEM_STORE_KEY else item.replace("-", ".") + ".dpapi"


def dpapi_entropy(item: str) -> bytes:
    return f"wa-mcp/{item}".encode()


def backend_name() -> str:
    if sys.platform == "win32":
        return "dpapi"
    return "keychain" if sys.platform == "darwin" else "secret-service"


def describe(store_dir: Path, item: str) -> str:
    """Where an item lives, for messages (never the secret)."""
    name = backend_name()
    if name == "dpapi":
        return f"Windows DPAPI ({store_dir / dpapi_file(item)})"
    if name == "keychain":
        return f"the macOS Keychain (service {SERVICE})"
    return f"the Secret Service (secret-tool, service {SERVICE})"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _BlobPtr = ctypes.POINTER(_Blob)
    _crypt32.CryptProtectData.argtypes = [
        _BlobPtr, wintypes.LPCWSTR, _BlobPtr, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, _BlobPtr,
    ]  # fmt: skip
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        _BlobPtr, ctypes.c_void_p, _BlobPtr, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, _BlobPtr,
    ]  # fmt: skip
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
        buf = ctypes.create_string_buffer(data, max(len(data), 1))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf

    def _take(out: _Blob) -> bytes:
        data = ctypes.string_at(out.pbData, out.cbData)
        ctypes.memset(out.pbData, 0, out.cbData)
        _kernel32.LocalFree(out.pbData)
        return data

    def protect(data: bytes, entropy: bytes) -> bytes:
        """DPAPI CryptProtectData for the current user (no machine scope)."""
        inp, keep_in = _blob(data)
        ent, keep_ent = _blob(entropy)
        out = _Blob()
        if not _crypt32.CryptProtectData(
            ctypes.byref(inp), "wa-mcp", ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)
        ):
            raise KeystoreError(f"DPAPI could not wrap the secret (error {ctypes.get_last_error()})")
        del keep_in, keep_ent
        return _take(out)

    def unprotect(data: bytes, entropy: bytes) -> bytes:
        inp, keep_in = _blob(data)
        ent, keep_ent = _blob(entropy)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)
        ):
            raise KeystoreError(f"DPAPI could not open the secret (error {ctypes.get_last_error()})")
        del keep_in, keep_ent
        return _take(out)

else:

    def protect(data: bytes, entropy: bytes) -> bytes:
        raise Unavailable("DPAPI exists on Windows only")

    def unprotect(data: bytes, entropy: bytes) -> bytes:
        raise Unavailable("DPAPI exists on Windows only")


def _dpapi_get(store_dir: Path, item: str) -> bytes | None:
    path = store_dir / dpapi_file(item)
    try:
        wrapped = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KeystoreError(f"cannot read {path}: {exc}") from exc
    try:
        return unprotect(wrapped, dpapi_entropy(item))
    except Unavailable:
        raise
    except KeystoreError as exc:
        raise KeystoreError(
            f"Windows DPAPI could not open {path}: it was made by another Windows user or on another "
            "computer, or it is damaged"
        ) from exc


def _store_id(store_dir: Path) -> str | None:
    path = store_dir / ID_FILE
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise KeystoreError(f"cannot read {path}") from exc
    if not _ID.match(value):
        raise KeystoreError(f"{path} is damaged")
    return value


def _cli_get(store_dir: Path, item: str, keychain: bool) -> bytes | None:
    program = "security" if keychain else "secret-tool"
    exe = shutil.which(program)
    if exe is None:
        raise Unavailable(f"{program} is not installed")
    sid = _store_id(store_dir)
    if sid is None:
        return None
    account = f"{item}.{sid}"
    if keychain:
        args = ["find-generic-password", "-s", SERVICE, "-a", account, "-w"]
    else:
        args = ["lookup", "service", SERVICE, "account", account]
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable(f"{program} did not run: {exc.__class__.__name__}") from exc
    if proc.returncode == 0:
        text = proc.stdout.decode("ascii", "replace").strip()
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise KeystoreError(f"the item {account} in {describe(store_dir, item)} is damaged") from exc
    if keychain and proc.returncode == 44:
        return None
    if not keychain and proc.returncode == 1 and not proc.stderr.strip():
        return None
    message = proc.stderr.decode("utf-8", "replace").strip()[:200] or f"exit status {proc.returncode}"
    raise Unavailable(f"{program}: {message}")


def get(store_dir: Path, item: str) -> bytes | None:
    """The secret of item for the store folder; None when it is not there.

    Raises Unavailable without a usable keystore and KeystoreError when the item is damaged
    or belongs to another user or computer.
    """
    name = backend_name()
    if name == "dpapi":
        return _dpapi_get(store_dir, item)
    return _cli_get(store_dir, item, keychain=name == "keychain")


def store_key(store_dir: Path) -> bytes | None:
    """The 32-byte store key of the folder, None when the keystore holds none."""
    key = get(store_dir, ITEM_STORE_KEY)
    if key is not None and len(key) != STORE_KEY_LEN:
        raise KeystoreError(f"the store key in {describe(store_dir, ITEM_STORE_KEY)} is damaged")
    return key


def parse_key(data: bytes) -> bytes:
    """32 raw bytes, 64 hex characters or base64 of 32 bytes (same rule as the bridge)."""
    if len(data) == STORE_KEY_LEN:
        return bytes(data)
    text = data.decode("ascii", "replace").strip()
    if len(text) == 2 * STORE_KEY_LEN:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) == STORE_KEY_LEN:
        return raw
    raise KeystoreError("want 32 bytes as 64 hex characters, base64 or raw bytes")


def read_key_file(path: str | os.PathLike[str]) -> bytes:
    """Read a key file with the checks of wa_mcp.secretfile (only the owner may read it, at most 4 KiB)."""
    p = Path(path)
    try:
        data = secretfile.read(p)
    except secretfile.SecretFileError as exc:
        raise KeystoreError(f"key file {exc}") from exc
    try:
        return parse_key(data)
    except KeystoreError as exc:
        raise KeystoreError(f"key file {p}: {exc}") from exc
