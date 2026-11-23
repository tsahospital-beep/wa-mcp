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

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "reference" / "tools.md"

GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Status",
        "Check that the setup works, and list the WhatsApp numbers when several are set up. Every tool takes "
        "an optional `account` (see [Several accounts](../guides/accounts.md)).",
        ("bridge_status", "list_accounts"),
    ),
    (
        "Contacts and chats",
        "Find people and chats. Chats can be given as a JID, a phone number with country code, or a name.",
        ("search_contacts", "list_chats", "get_chat", "get_last_interaction"),
    ),
    (
        "Messages",
        "Read stored messages. Voice transcripts and extracted file text appear inline.",
        (
            "list_messages",
            "get_message_context",
            "mark_as_read",
            "export_chat",
            "export_period",
        ),
    ),
    (
        "History",
        "Ask the phone for messages older than the ones stored.",
        ("request_chat_history", "sync_chat_history", "sync_all_history"),
    ),
    (
        "Media and files",
        "Download attachments, read images and documents, or inspect shared links.",
        (
            "download_media",
            "download_chat_media",
            "get_media_text",
            "list_stickers",
            "extract_file_text",
            "fetch_shared_docs",
            "check_links",
            "read_images",
        ),
    ),
    ("Chat imports", "Add older messages from a local WhatsApp export.", ("import_chat_export",)),
    (
        "Voice transcription",
        "Turn voice notes and audio messages into text with the configured backend.",
        ("transcribe_chat_audio",),
    ),
    (
        "Agent connector",
        "Review and control the external reply provider for one account.",
        ("agent_status", "agent_test", "agent_drafts", "agent_approve_draft", "agent_pause", "agent_resume"),
    ),
    (
        "Calls",
        "Read the call log.",
        ("list_calls",),
    ),
    (
        "Sending",
        "Off by default. Each call needs `sending.enabled`. With `sending.require_confirm` the server asks the user "
        "through the client (MCP elicitation). The fallback also requires `sending.allow_confirm_flag`.",
        ("send_message", "send_file", "send_voice_note", "send_sticker"),
    ),
    (
        "Scheduled messages",
        "Messages the bridge sends later, once or on a repeat.",
        ("schedule_message", "list_scheduled", "cancel_scheduled"),
    ),
    (
        "Analytics and tasks",
        "Analyze the local archive. Only the optional summary in `my_day` uses a model provider.",
        (
            "my_day",
            "chat_analytics",
            "chat_analytics_compare",
            "detect_tasks",
            "tasks_overview",
            "task_detail",
            "which_task",
            "refresh_tasks",
            "project_overview",
            "whats_new",
            "find_mentions",
        ),
    ),
)

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
        f"The server exposes {len(tools)} tools over stdio. Arguments marked `required` have no default.",
        "",
        "Server instructions sent to the client:",
        "",
        f"> {clean(instructions)}",
        "",
    ]
    for title, blurb, names in GROUPS:
        present = [n for n in names if n in by_name]
        if not present:
            continue
        out += [f"## {title}", "", blurb, ""]
        for name in present:
            out.append(render_tool(by_name[name]))
            used.add(name)
    rest = [t for t in tools if t.name not in used]
    if rest:
        out += ["## Other", ""]
        out += [render_tool(t) for t in rest]
    return "\n".join(out).rstrip() + "\n"


async def collect() -> str:
    with tempfile.TemporaryDirectory(prefix="wa-mcp-docs-") as tmp:
        config = Path(tmp) / "config.json"
        config.write_text("{}\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WA_MCP_")}
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "wa_mcp_server", "--config", str(config)],
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
