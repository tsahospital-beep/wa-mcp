"""Private files for anything that may hold message text (exports, reports, insight.db, sync state).

Folders are created 0700 and files 0600. On Windows the mode bits only control the read-only flag and
the folder's ACL decides who can read; keep the store and export folders inside the user profile.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def private_dir(path: Path) -> Path:
    """Create path (and parents) and restrict it to the owner where the OS supports it."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def restrict(path: Path) -> None:
    """chmod 0600 an existing file (also the -wal and -shm files of a SQLite database)."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            with contextlib.suppress(OSError):
                os.chmod(p, 0o600)


def write_private(path: Path, text: str) -> Path:
    """Write text to path with mode 0600 from the start (no window where it is world-readable)."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8"))
    restrict(path)
    return path


def file_stem(text: str, limit: int = 60) -> str:
    """ASCII file name part for any chat name or JID; a short hash keeps non-Latin names apart."""
    ascii_part = _UNSAFE.sub("_", text).strip("._")[:limit]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part}_{digest}" if ascii_part else f"chat_{digest}"
