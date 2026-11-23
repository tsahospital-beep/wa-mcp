"""Test helpers for the image text tests: a tiny PNG and a fake tesseract program."""

from __future__ import annotations

import os
import struct
import sys
import zlib
from pathlib import Path

import pytest


def png(width: int = 1, height: int = 1) -> bytes:
    """A valid grey PNG header of this size (the pixel data is one row; decoders are not used here)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + chunk(b"IEND", b"")
    )


PNG = png()
SHOT_TEXT = "Invoice 4711\n\n\n\nDue   Friday  \n"


def fake_tesseract(folder: Path, out: str = SHOT_TEXT, code: int = 0, err: str = "") -> Path:
    """A tesseract program on disk: records its arguments, prints `out`."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "fake_tesseract.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).with_name('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.buffer.write({out.encode('utf-8')!r})\n"
        f"sys.stderr.write({err!r})\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = folder / "tesseract.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
    else:
        wrapper = folder / "tesseract"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def on_path(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
