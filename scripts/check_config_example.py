"""Check that config.example.json lists every key with its default value.

Three sources of defaults are compared with the example file and with each other:

* the Go bridge (bridge/internal/config, read by running a small program through `go run -overlay`,
  so nothing is written into the repository),
* the Python package (wa_mcp.config.DEFAULTS),
* the insight settings that the Python code applies when a key is missing.

The example must also load without errors and without "unknown key" warnings in the bridge.
Requires Go on PATH. Pass --skip-go to check the Python side only.

    uv run --all-packages python scripts/check_config_example.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from wa_mcp import config, db
from wa_mcp.console import configure_console
from wa_mcp.insight import base
from wa_mcp_server.toolsets import parse as parse_toolsets

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge"
EXAMPLE = ROOT / "config.example.json"

GO_PROGRAM = """package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/config"
)

func main() {
	out := map[string]any{"defaults": config.Default()}
	cfg, err := config.Load(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	out["warnings"] = cfg.Warnings
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		os.Exit(1)
	}
}
"""


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf values by dotted path. Empty objects and all lists are leaves."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else key))
        return out
    return {prefix: value}


def insight_defaults() -> dict[str, Any]:
    """Defaults that the insight modules apply when a key is missing, read from a real empty config."""
    with tempfile.TemporaryDirectory(prefix="wa-mcp-config-check-") as tmp:
        path = Path(tmp) / "config.json"
        path.write_text("{}\n", encoding="utf-8")
        db.connect(config.load(path))
        s = base.settings()
    return {
        "tasks.burst_gap_minutes": s.burst_gap_minutes,
        "tasks.reply_window_minutes": s.reply_window_minutes,
        "tasks.refresh_days": s.refresh_days,
        "tasks.min_score": s.min_task_score,
        "tasks.stale_days": s.stale_days,
        "tasks.keep_days": s.keep_days,
        "tasks.include_imported": s.include_imported,
        "insight.night_hours": list(s.night),
        "insight.date_order": s.date_order,
        "insight.turn_gap_seconds": s.turn_gap_seconds,
        "insight.session_gap_hours": s.session_gap_hours,
        "insight.cache_days": s.cache_days,
        "insight.my_names": list(s.my_names.items),
        "insight.words": {},
        "insight.dates": {},
    }


def go_defaults() -> tuple[dict[str, Any], list[str]]:
    """Defaults of the Go bridge and the warnings it prints for the example file."""
    go = shutil.which("go")
    if go is None:
        raise RuntimeError("go was not found on PATH (use --skip-go to check the Python side only)")
    with tempfile.TemporaryDirectory(prefix="wa-mcp-go-defaults-") as tmp:
        program = Path(tmp) / "main.go"
        program.write_text(GO_PROGRAM, encoding="utf-8")
        overlay = Path(tmp) / "overlay.json"
        target = BRIDGE / "cmd" / "dumpdefaults" / "main.go"
        overlay.write_text(json.dumps({"Replace": {str(target): str(program)}}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WA_MCP_")}
        proc = subprocess.run(
            [go, "run", f"-overlay={overlay}", "./cmd/dumpdefaults", str(EXAMPLE)],
            cwd=BRIDGE,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"go run failed: {proc.stderr.strip() or proc.stdout.strip()}")
    data = json.loads(proc.stdout)
    return flatten(data["defaults"]), [str(w) for w in data.get("warnings") or []]


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Check config.example.json against the defaults")
    parser.add_argument("--skip-go", action="store_true", help="do not run the Go program")
    args = parser.parse_args()

    example = flatten(json.loads(EXAMPLE.read_text(encoding="utf-8")))
    sources: dict[str, dict[str, Any]] = {
        "python": flatten(config.DEFAULTS),
        "insight": insight_defaults(),
    }
    problems: list[str] = []
    if not args.skip_go:
        try:
            sources["go"], warnings = go_defaults()
        except RuntimeError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        problems += [f"the bridge warns: {w}" for w in warnings]

    for key in ("login.device_name", "login.device_platform"):
        for source in ("python", "go"):
            if source in sources and key not in sources[source]:
                problems.append(f"{source} has no default for {key}")

    expected: dict[str, Any] = {}
    owner: dict[str, str] = {}
    for name, values in sources.items():
        for key, value in values.items():
            if key in expected and expected[key] != value:
                problems.append(
                    f"{key}: {owner[key]} default is {expected[key]!r}, {name} default is {value!r}"
                )
            expected.setdefault(key, value)
            owner.setdefault(key, name)

    for key, value in expected.items():
        if key not in example:
            problems.append(f"missing in the example: {key} (default {value!r}, from {owner[key]})")
        elif example[key] != value:
            problems.append(f"{key}: example has {example[key]!r}, default is {value!r} (from {owner[key]})")
    if not args.skip_go:
        problems += [f"unknown key in the example: {key}" for key in example if key not in expected]
    parse_toolsets(config.load(EXAMPLE).section("mcp").get("toolsets"))
    if problems:
        sys.stderr.write("config.example.json does not match the defaults:\n")
        sys.stderr.writelines(f"  {p}\n" for p in problems)
        return 1
    checked = "python, insight" + ("" if args.skip_go else ", go")
    sys.stdout.write(f"config.example.json matches the defaults ({len(expected)} keys; sources: {checked})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
