"""Calls made to the AI provider per local day, kept in store/insight.db (no message text).

Every call is counted before it is sent, whether it succeeds or not, because a failed call can still be
billed. ai.max_calls_per_day = 0 turns the limit off.
"""

from __future__ import annotations

import time
from datetime import datetime

from wa_mcp.ai.base import AILimitError, Meter
from wa_mcp.insight import state

KEEP_DAYS = 60


def _day(now: float | None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now).date().isoformat()


def used_today(now: float | None = None) -> int:
    with state.connect() as conn:
        row = conn.execute("SELECT calls FROM ai_usage WHERE day = ?", (_day(now),)).fetchone()
    return int(row[0]) if row else 0


def reserve(limit: int, now: float | None = None) -> int:
    """Count one call for today and return the new count; AILimitError when the limit is already used up."""
    day = _day(now)
    cutoff = _day((time.time() if now is None else now) - KEEP_DAYS * 86400)
    with state.connect() as conn, state.transaction(conn):
        row = conn.execute("SELECT calls FROM ai_usage WHERE day = ?", (day,)).fetchone()
        used = int(row[0]) if row else 0
        if limit > 0 and used >= limit:
            raise AILimitError(
                f"{used} AI calls were made today, the limit ai.max_calls_per_day is {limit}; "
                "raise it or try again tomorrow"
            )
        conn.execute(
            "INSERT INTO ai_usage (day, calls) VALUES (?, 1) ON CONFLICT (day) DO UPDATE SET calls = calls + 1",
            (day,),
        )
        conn.execute("DELETE FROM ai_usage WHERE day < ?", (cutoff,))
    return used + 1


def meter(limit: int) -> Meter:
    """The counter a provider calls before each request."""

    def count() -> None:
        reserve(limit)

    return count
