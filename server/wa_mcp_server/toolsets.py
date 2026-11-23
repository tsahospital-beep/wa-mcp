"""Tool names advertised by each MCP toolset."""

from __future__ import annotations

import os

from wa_mcp import config

TOOLSETS = {
    "core": (
        "bridge_status",
        "list_chats",
        "search_contacts",
        "get_chat",
        "list_messages",
        "get_message_context",
        "my_day",
        "send_message",
        "send_file",
        "download_media",
        "transcribe_chat_audio",
    ),
    "history": (
        "get_last_interaction",
        "mark_as_read",
        "list_calls",
        "export_chat",
        "export_period",
        "request_chat_history",
        "sync_chat_history",
        "sync_all_history",
        "import_chat_export",
    ),
    "media": (
        "download_chat_media",
        "get_media_text",
        "list_stickers",
        "extract_file_text",
        "fetch_shared_docs",
        "check_links",
        "read_images",
        "send_voice_note",
        "send_sticker",
    ),
    "insight": ("chat_analytics", "chat_analytics_compare", "find_mentions"),
    "tasks": (
        "detect_tasks",
        "tasks_overview",
        "task_detail",
        "which_task",
        "refresh_tasks",
        "project_overview",
        "whats_new",
    ),
    "schedule": ("schedule_message", "list_scheduled", "cancel_scheduled"),
    "agent": (
        "agent_status",
        "agent_test",
        "agent_drafts",
        "agent_approve_draft",
        "agent_pause",
        "agent_resume",
    ),
    "accounts": ("list_accounts",),
    "actions": ("react_to_message", "edit_message", "delete_message", "forward_message"),
}


def enabled(cfg: config.Config, override: str | None = None) -> tuple[str, ...]:
    raw = override if override is not None else os.environ.get("WA_MCP_TOOLSETS")
    names = [n.strip() for n in raw.split(",")] if raw is not None else cfg.section("mcp").get("toolsets")
    return parse(names)


def parse(names: object) -> tuple[str, ...]:
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise config.ConfigError("mcp.toolsets must be a nonempty list of toolset names")
    unknown = set(names) - TOOLSETS.keys() - {"all"}
    if unknown:
        raise config.ConfigError("unknown MCP toolsets: " + ", ".join(sorted(unknown)))
    return tuple(TOOLSETS) if "all" in names else tuple(dict.fromkeys(names))
