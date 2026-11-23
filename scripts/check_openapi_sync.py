"""Check that docs/reference/openapi.yaml is identical to the spec the bridge embeds.

The bridge serves bridge/internal/api/openapi.yaml at /api/v1/openapi.yaml. The copy under docs/reference
is for readers. Copy the embedded file over it after every change:

    cp bridge/internal/api/openapi.yaml docs/reference/openapi.yaml
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMBEDDED = ROOT / "bridge" / "internal" / "api" / "openapi.yaml"
COPY = ROOT / "docs" / "reference" / "openapi.yaml"


def read(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def main() -> int:
    for path in (EMBEDDED, COPY):
        if not path.is_file():
            sys.stderr.write(f"missing: {path.relative_to(ROOT)}\n")
            return 1
    if read(EMBEDDED) != read(COPY):
        sys.stderr.write(
            f"{COPY.relative_to(ROOT)} differs from {EMBEDDED.relative_to(ROOT)}.\n"
            f"Run: cp {EMBEDDED.relative_to(ROOT)} {COPY.relative_to(ROOT)}\n"
        )
        return 1
    sys.stdout.write("openapi.yaml copies are identical\n")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
