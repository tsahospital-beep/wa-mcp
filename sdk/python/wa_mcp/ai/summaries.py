"""Summary of the my_day result written by the configured provider.

Only the structured fields go to the provider: counts, person or group names, item types, due hints, wait
times and the short context line of each item (clipped). Everything from a chat is untrusted text and is
wrapped as such in the prompt.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from wa_mcp.ai import NOT_CONFIGURED, configured, get_provider, prompts
from wa_mcp.ai.base import AIError, AILimitError, Provider

CONTEXT_CHARS = 120
NAME_CHARS = 60
SUMMARY_TOKENS = 600
_DROP = frozenset(("Cc", "Cf", "Zl", "Zp"))
_ITEM_FIELDS = (
    "status",
    "due",
    "due_status",
    "waiting_minutes",
    "unanswered",
    "question",
    "missed",
    "video",
    "recurrence",
)


def _line(text: object, limit: int) -> str:
    """One line without control, separator or direction characters, clipped."""
    kept = (" " if unicodedata.category(c) in _DROP else c for c in str(text))
    return " ".join("".join(kept).split())[:limit]


def digest(res: dict[str, Any], budget: int) -> str:
    """The structured result as compact lines, most urgent first, cut at `budget` characters."""
    counts = ", ".join(f"{k}={v}" for k, v in res.get("counts", {}).items() if v)
    lines = [f"counts: {counts or 'none'}"]
    size = len(lines[0])
    people = res.get("people", [])
    for shown, group in enumerate(people):
        block = [f"{_line(group.get('name', ''), NAME_CHARS)}{' (group)' if group.get('is_group') else ''}:"]
        for item in group.get("items", []):
            parts = [str(item.get("type", ""))]
            parts += [f"{k}={item[k]}" for k in _ITEM_FIELDS if item.get(k) not in (None, "", False)]
            if item.get("context"):
                parts.append(f'text="{_line(item["context"], CONTEXT_CHARS)}"')
            block.append("  " + " ".join(parts))
        added = sum(len(x) + 1 for x in block)
        if size + added > budget:
            lines.append(f"({len(people) - shown} more people or groups left out)")
            break
        lines += block
        size += added
    return "\n".join(lines)


def summarize_day(res: dict[str, Any], provider: Provider) -> str:
    """A short briefing for the day in `res` (the my_day result)."""
    overhead = len(prompts.SUMMARY_SYSTEM) + len(prompts.SUMMARY_TASK) + 200
    budget = provider.cfg.max_input_chars - overhead
    if budget < 500:
        raise AILimitError("ai.max_input_chars is too small for a summary; use at least 2000")
    data = digest(res, budget)
    return provider.complete(
        prompts.summary_prompt(str(res.get("date", "")), data), prompts.SUMMARY_SYSTEM, SUMMARY_TOKENS
    )


def day_summary(res: dict[str, Any]) -> dict[str, Any]:
    """Fields to add to the my_day result when a summary was asked for. Never raises for provider problems."""
    if not configured():
        return {"ai_note": NOT_CONFIGURED}
    if not res.get("people"):
        return {
            "ai_summary": f"Nothing needs attention on {res.get('date', 'this day')}.",
            "ai_provider": None,
        }
    try:
        provider = get_provider()
        return {"ai_summary": summarize_day(res, provider), "ai_provider": provider.name}
    except AIError as exc:
        return {"ai_summary": None, "ai_error": str(exc)}
