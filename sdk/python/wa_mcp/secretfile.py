"""Secret files (key files, passphrase files from Docker secrets), with the same checks as the bridge
(bridge/internal/keystore ReadSecretFile): a regular file of at most 4 KiB that only its owner can read.
On macOS and Linux the mode must have no group or other bits. On Windows the access list may grant
access only to the current user and SYSTEM; anything else (Administrators, Users, Everyone, OWNER RIGHTS)
is refused with an icacls hint.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

MAX_SECRET_FILE = 4096


class SecretFileError(Exception):
    """The file is missing, too large or readable by others."""


def windows_hint(path: Path) -> str:
    return (
        f'limit it to your user and SYSTEM: icacls "{path}" /inheritance:r /remove:g *S-1-5-32-544 *S-1-3-4 '
        '*S-1-5-11 *S-1-5-32-545 *S-1-1-0 /grant:r "%USERNAME%:R" SYSTEM:R'
    )


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _PVOID = ctypes.c_void_p
    _advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
        ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
    ]  # fmt: skip
    _advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi.OpenProcessToken.restype = wintypes.BOOL
    _advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        _PVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi.GetTokenInformation.restype = wintypes.BOOL
    _advapi.CreateWellKnownSid.argtypes = [ctypes.c_int, _PVOID, _PVOID, ctypes.POINTER(wintypes.DWORD)]
    _advapi.CreateWellKnownSid.restype = wintypes.BOOL
    _advapi.GetAce.argtypes = [_PVOID, wintypes.DWORD, ctypes.POINTER(_PVOID)]
    _advapi.GetAce.restype = wintypes.BOOL
    _advapi.EqualSid.argtypes = [_PVOID, _PVOID]
    _advapi.EqualSid.restype = wintypes.BOOL
    _advapi.ConvertSidToStringSidW.argtypes = [_PVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    _kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.LocalFree.argtypes = [_PVOID]
    _kernel.LocalFree.restype = _PVOID
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 4
    _TOKEN_QUERY = 8
    _TOKEN_USER = 1
    _WIN_LOCAL_SYSTEM_SID = 22
    _ACCESS_ALLOWED_ACE_TYPE = 0

    def _fail(what: str) -> SecretFileError:
        return SecretFileError(f"{what} (Windows error {ctypes.get_last_error()})")

    def _user_sid() -> ctypes.Array[ctypes.c_char]:
        token = wintypes.HANDLE()
        if not _advapi.OpenProcessToken(_kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise _fail("cannot open the process token")
        try:
            size = wintypes.DWORD()
            _advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
                raise _fail("cannot read the current user")
            return buf  # TOKEN_USER: the first field is a pointer to the user's SID inside buf
        finally:
            _kernel.CloseHandle(token)

    def _sid_text(sid: int) -> str:
        text = wintypes.LPWSTR()
        if not _advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return "another account"
        try:
            return str(text.value)
        finally:
            _kernel.LocalFree(ctypes.cast(text, _PVOID))

    def check_private(path: Path) -> None:
        dacl, sd = _PVOID(), _PVOID()
        rc = _advapi.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None, ctypes.byref(dacl), None,
            ctypes.byref(sd),
        )  # fmt: skip
        if rc != 0:
            raise SecretFileError(f"cannot read the access list of {path} (Windows error {rc})")
        try:
            if not dacl.value:
                raise SecretFileError(
                    f"{path} has no access list, so everyone may read it; {windows_hint(path)}"
                )
            user_buf = _user_sid()
            user = _PVOID.from_buffer(user_buf).value
            system = ctypes.create_string_buffer(68)
            size = wintypes.DWORD(len(system))
            if not _advapi.CreateWellKnownSid(_WIN_LOCAL_SYSTEM_SID, None, system, ctypes.byref(size)):
                raise _fail("cannot build the SYSTEM account id")
            count = ctypes.c_ushort.from_address(dacl.value + 4).value  # ACL.AceCount
            for i in range(count):
                ace = _PVOID()
                if not _advapi.GetAce(dacl, i, ctypes.byref(ace)) or not ace.value:
                    raise _fail(f"cannot read the access list of {path}")
                if ctypes.c_ubyte.from_address(ace.value).value != _ACCESS_ALLOWED_ACE_TYPE:
                    continue
                sid = ace.value + 8  # ACE_HEADER (4) and the access mask (4), then the SID
                if _advapi.EqualSid(sid, user) or _advapi.EqualSid(sid, ctypes.addressof(system)):
                    continue
                raise SecretFileError(f"{path} can be read by {_sid_text(sid)}; {windows_hint(path)}")
        finally:
            _kernel.LocalFree(sd)

else:

    def check_private(path: Path) -> None:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise SecretFileError(f"{path} can be read by other users (mode {mode:04o}); run chmod 600 on it")


def read(path: str | os.PathLike[str]) -> bytes:
    """The content of a secret file after the checks above."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise SecretFileError(f"{p}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise SecretFileError(f"{p} is not a regular file")
    if st.st_size > MAX_SECRET_FILE:
        raise SecretFileError(f"{p} is larger than {MAX_SECRET_FILE} bytes")
    check_private(p)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise SecretFileError(f"{p}: {exc.strerror or exc}") from exc
    if len(data) > MAX_SECRET_FILE:
        raise SecretFileError(f"{p} is larger than {MAX_SECRET_FILE} bytes")
    return data


def read_passphrase(path: str | os.PathLike[str]) -> str:
    """A passphrase file: one trailing newline (LF or CRLF) is removed, nothing else, as in the bridge."""
    text = read(path).decode("utf-8")
    text = text.removesuffix("\r\n") if text.endswith("\r\n") else text.removesuffix("\n")
    if not text:
        raise SecretFileError(f"{path} is empty")
    return text


def passphrase(name: str) -> str:
    """The passphrase in the variable `name` (used as it is), or else from the file in `name`_FILE."""
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value:
        return value
    file = os.environ.get(name + "_FILE", "")
    return read_passphrase(file) if file else ""
