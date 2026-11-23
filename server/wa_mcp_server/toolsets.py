"""Tool names advertised by each MCP toolset."""

from __future__ import annotations

import os

from wa_mcp import config

TOOLSETS = {
    "core": (
        "get_bridge_status",
        "list_chats",
        "search_contacts",
        "get_chat",
        "list_messages",
        "get_message_context",
        "get_my_day",
        "send_message",
        "send_file",
        "download_media",
        "transcribe_chat_audio",
    ),
    "history": (
        "get_last_interaction",
        "mark_messages_read",
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
    "insight": ("get_chat_analytics", "compare_chat_analytics", "find_mentions"),
    "tasks": (
        "detect_tasks",
        "list_tasks",
        "get_task",
        "find_task_for_message",
        "refresh_tasks",
        "get_project_overview",
        "list_whats_new",
    ),
    "schedule": ("schedule_message", "list_scheduled_messages", "cancel_scheduled_message"),
    "agent": (
        "get_agent_status",
        "test_agent_provider",
        "list_agent_drafts",
        "approve_agent_draft",
        "pause_agent",
        "resume_agent",
    ),
    "accounts": ("list_accounts",),
    "actions": ("set_message_reaction", "edit_message", "delete_message", "forward_message"),
}


LEGACY_NAMES = {
    "bridge_status": "get_bridge_status",
    "my_day": "get_my_day",
    "chat_analytics": "get_chat_analytics",
    "chat_analytics_compare": "compare_chat_analytics",
    "tasks_overview": "list_tasks",
    "task_detail": "get_task",
    "which_task": "find_task_for_message",
    "project_overview": "get_project_overview",
    "whats_new": "list_whats_new",
    "agent_status": "get_agent_status",
    "agent_test": "test_agent_provider",
    "agent_drafts": "list_agent_drafts",
    "agent_approve_draft": "approve_agent_draft",
    "agent_pause": "pause_agent",
    "agent_resume": "resume_agent",
    "mark_as_read": "mark_messages_read",
    "list_scheduled": "list_scheduled_messages",
    "cancel_scheduled": "cancel_scheduled_message",
    "react_to_message": "set_message_reaction",
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
