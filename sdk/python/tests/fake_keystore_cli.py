"""Fake `security` and `secret-tool` programs for test_keystore.py.

Run as: python fake_keystore_cli.py <program> <args...>. Items live in the JSON file named by
FAKE_KEYSTORE_DB ({"service/account": "hex value"}); every argument list is appended to
FAKE_KEYSTORE_DB + ".argv". FAKE_KEYSTORE_FAIL=1 makes the program fail like a missing D-Bus.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    program, args = sys.argv[1], sys.argv[2:]
    db = Path(os.environ["FAKE_KEYSTORE_DB"])
    with open(str(db) + ".argv", "a", encoding="utf-8") as log:
        log.write(" ".join([program, *args]) + "\n")
    items = json.loads(db.read_text(encoding="utf-8")) if db.is_file() else {}
    if os.environ.get("FAKE_KEYSTORE_FAIL"):
        sys.stderr.write(f"{program}: Cannot autolaunch D-Bus without X11 $DISPLAY\n")
        return 1
    if program == "security":
        flags = dict(zip(args[1::2], args[2::2], strict=False))
        key = f"{flags.get('-s')}/{flags.get('-a')}"
        if args[:1] == ["find-generic-password"]:
            if key not in items:
                sys.stderr.write("security: The specified item could not be found in the keychain.\n")
                return 44
            sys.stdout.write(items[key] + "\n")
            return 0
        return 2
    if args[:1] == ["lookup"] and len(args) == 5:
        key = f"{args[2]}/{args[4]}"
        if key not in items:
            return 1
        sys.stdout.write(items[key])
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
