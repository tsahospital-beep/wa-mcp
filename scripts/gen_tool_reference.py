"""Generate docs/reference/tools.md from the live MCP server.

The script starts `python -m wa_mcp_server` over stdio with a temporary empty config,
lists the tools through the MCP client and prints the Markdown page.

    uv run python scripts/gen_tool_reference.py            # print to stdout
    uv run python scripts/gen_tool_reference.py --write    # write docs/reference/tools.md
    uv run python scripts/gen_tool_reference.py --check    # exit 1 when the file is out of date
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wa_mcp.console import configure_console
from wa_mcp_server.toolsets import TOOLSETS

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "reference" / "tools.md"

TYPE_NAMES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "null": "null",
}


def type_of(schema: dict[str, Any]) -> str:
    """Short type name from a JSON schema fragment."""
    if "anyOf" in schema:
        parts = [type_of(s) for s in schema["anyOf"] if s.get("type") != "null"]
        return " or ".join(dict.fromkeys(parts)) or "any"
    kind = schema.get("type")
    if kind == "array":
        return f"list of {type_of(schema.get('items', {}))}"
    if isinstance(kind, str):
        return TYPE_NAMES.get(kind, kind)
    return "any"


def default_of(name: str, schema: dict[str, Any], required: set[str]) -> str:
    if name in required:
        return "required"
    if "default" not in schema:
        return ""
    value = schema["default"]
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"`{value}`"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_description(text: str) -> tuple[str, str]:
    """First paragraph as the summary, the remaining paragraphs as details."""
    paragraphs = [clean(p) for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paragraphs:
        return "", ""
    return paragraphs[0], "\n\n".join(paragraphs[1:])


def hint_of(tool: Any) -> str:
    """What the tool annotations tell a client, in words."""
    a = getattr(tool, "annotations", None)
    if a is None:
        return ""
    labels = ["read-only" if a.readOnlyHint else "may change state"]
    if a.destructiveHint:
        labels.append("potentially destructive")
    labels.append("idempotent" if a.idempotentHint else "retries can have additional effects")
    labels.append("open world" if a.openWorldHint else "closed world")
    return ", ".join(labels)


def render_tool(tool: Any) -> str:
    summary, details = split_description(tool.description or "")
    lines = [f"### `{tool.name}`", "", summary, ""]
    hint = hint_of(tool)
    if hint:
        lines += [f"**{tool.annotations.title}** ({hint}).", ""]
    props: dict[str, Any] = (tool.inputSchema or {}).get("properties", {})
    required = set((tool.inputSchema or {}).get("required", []))
    if props:
        lines += ["| Argument | Type | Default | Description |", "| --- | --- | --- | --- |"]
        for name, schema in props.items():
            description = clean(schema["description"]).replace("|", "\\|")
            lines.append(
                f"| `{name}` | {type_of(schema)} | {default_of(name, schema, required)} | {description} |"
            )
        lines.append("")
    else:
        lines += ["No arguments.", ""]
    if details:
        lines += [details, ""]
    return "\n".join(lines)


def render(tools: list[Any], instructions: str) -> str:
    by_name = {t.name: t for t in tools}
    used: set[str] = set()
    out = [
        "# MCP tools",
        "",
        "This page is generated from the running server by `scripts/gen_tool_reference.py`.",
        f"The core toolset exposes {len(TOOLSETS['core'])} tools by default. Select `all` for all {len(tools)} tools. Arguments marked `required` have no default.",
        "",
        "Server instructions sent to the client:",
        "",
        f"> {clean(instructions)}",
        "",
    ]
    for title, names in TOOLSETS.items():
        out += [
            f"## {title}",
            "",
            "Enabled by default." if title == "core" else "Enable this toolset by name.",
            "",
        ]
        for name in names:
            out.append(render_tool(by_name[name]))
            used.add(name)
    if used != set(by_name):
        raise ValueError(f"tools missing from toolsets: {set(by_name) - used}")
    return "\n".join(out).rstrip() + "\n"


async def collect() -> str:
    with tempfile.TemporaryDirectory(prefix="wa-mcp-docs-") as tmp:
        config = Path(tmp) / "config.json"
        config.write_text("{}\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WA_MCP_")}
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "wa_mcp_server", "--config", str(config), "--toolsets", "all"],
            env=env,
            cwd=tmp,
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            listing = await session.list_tools()
            return render(list(listing.tools), init.instructions or "")


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Generate docs/reference/tools.md from the live server")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help=f"write {TARGET.relative_to(ROOT)}")
    mode.add_argument("--check", action="store_true", help="exit 1 when the file differs from the server")
    args = parser.parse_args()
    text = asyncio.run(collect())
    if args.write:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(text, encoding="utf-8", newline="\n")
        sys.stdout.write(f"wrote {TARGET.relative_to(ROOT)}\n")
        return 0
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current.replace("\r\n", "\n") != text:
            sys.stderr.write(
                f"{TARGET.relative_to(ROOT)} is out of date: run scripts/gen_tool_reference.py --write\n"
            )
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
