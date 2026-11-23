"""Fetch older messages from the phone through the bridge.

The phone answers history requests in the background; the bridge stores the
messages in wa.db. sync_chat_history pages back until the oldest stored message
reaches the wanted date, the page cap or time budget is hit, or the phone sent
nothing older for EXHAUST_AFTER requests in a row (the chat probably begins
after the wanted date): that chat is "exhausted" and not asked again.

sync_all_history keeps its progress in store/insight.db (table history_sync,
created by wa_mcp.insight.state migrations) so a later call resumes. It is
complete when every chat is reached or exhausted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from wa_mcp import db, people
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.db import TimeArg, to_unix
from wa_mcp.insight import state

log = logging.getLogger(__name__)

Sleeper = Callable[[float], None]
Clock = Callable[[], float]
DEFAULT_BUDGET_SECONDS = 120.0
EXHAUST_AFTER = 3
TIME_BUDGET = "time budget used up"
EXHAUSTED = "exhausted: the phone sent nothing older"


@dataclass
class SyncResult:
    chat: str
    pages: int
    oldest: int | None
    oldest_time: str | None
    reached: bool
    stopped: str
    empty_pages: int = 0
    exhausted: bool = False

    @property
    def state(self) -> str:
        return "reached" if self.reached else "exhausted" if self.exhausted else "pending"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state}


def request_history(
    bridge: Bridge, chat: str, count: int = 50, before_id: str | None = None
) -> dict[str, Any]:
    """One on-demand history request (nothing is sent to the contact)."""
    return bridge.request_history(chat, count=count, before_id=before_id)


def _oldest(convs: list[str]) -> tuple[int | None, str | None, str | None]:
    rows = db.messages(conv=convs, ascending=True, limit=1)
    if not rows:
        return None, None, None
    return int(rows[0]["sent_at"]), str(rows[0]["msg_id"]), str(rows[0]["conv"])


def sync_chat_history(
    bridge: Bridge,
    chat: str,
    back_to: TimeArg,
    max_pages: int = 20,
    page_size: int = 50,
    wait_seconds: float = 20.0,
    poll_seconds: float = 2.0,
    sleep: Sleeper = time.sleep,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    clock: Clock = time.monotonic,
    empty_before: int = 0,
    exhaust_after: int = EXHAUST_AFTER,
) -> SyncResult:
    """Page back through one chat (LID and phone forms together) until back_to, exhaustion or the budget.

    empty_before carries the count of earlier requests in a row that brought nothing older.
    """
    target = to_unix(back_to)
    if target is None:
        raise ValueError("back_to is required")
    deadline = clock() + budget_seconds
    convs = people.same_person(chat)
    pages = 0
    empty = max(empty_before, 0)
    oldest, oldest_id, oldest_conv = _oldest(convs)
    stopped = "page cap"
    while pages < max_pages:
        if oldest is not None and oldest <= target:
            break
        if empty >= exhaust_after:
            stopped = EXHAUSTED
            break
        if clock() >= deadline:
            stopped = TIME_BUDGET
            break
        try:
            bridge.request_history(oldest_conv or convs[0], count=page_size, before_id=oldest_id)
        except BridgeError as exc:
            stopped = f"bridge: {exc.message}"
            break
        pages += 1
        waited = 0.0
        moved = False
        step = max(min(poll_seconds, wait_seconds), 0.05)
        while waited < wait_seconds and clock() < deadline:
            sleep(step)
            waited += step
            now_oldest, now_id, now_conv = _oldest(convs)
            if now_oldest is not None and (oldest is None or now_oldest < oldest):
                oldest, oldest_id, oldest_conv = now_oldest, now_id, now_conv
                moved = True
                break
        if moved:
            empty = 0
        elif clock() >= deadline:
            stopped = TIME_BUDGET
            break
        else:
            empty += 1
    reached = oldest is not None and oldest <= target
    exhausted = not reached and empty >= exhaust_after
    if reached:
        stopped = "reached"
    elif exhausted:
        stopped = EXHAUSTED
    return SyncResult(chat, pages, oldest, db.iso_local(oldest), reached, stopped, empty, exhausted)


def sync_all_history(
    bridge: Bridge,
    active_since: TimeArg,
    back_to: TimeArg,
    max_pages_per_chat: int = 10,
    max_chats: int = 200,
    restart: bool = False,
    sleep: Sleeper = time.sleep,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    clock: Clock = time.monotonic,
    **kwargs: Any,
) -> dict[str, Any]:
    """sync_chat_history for every chat active since active_since, within one time budget.

    A chat is done when it reached back_to (or an earlier date) or is exhausted. Calling again with the
    same arguments continues with the others; complete=true when none are left.
    """
    target = to_unix(back_to)
    if target is None:
        raise ValueError("back_to is required")
    deadline = clock() + budget_seconds
    with state.connect() as conn:
        if restart:
            conn.execute("DELETE FROM history_sync")
        saved: dict[str, dict[str, Any]] = {
            str(r["conv"]): dict(r)
            for r in conn.execute("SELECT conv, back_to, empty_pages, state FROM history_sync").fetchall()
        }

        def finished(key: str) -> bool:
            row = saved.get(key)
            if row is None:
                return False
            return bool(
                row["state"] == "exhausted" or (row["state"] == "reached" and row["back_to"] <= target)
            )

        keys: list[str] = []
        for chat in db.active_chats(after=active_since):
            key = people.same_person(chat["jid"])[0]
            if key not in keys:
                keys.append(key)
        pending = [k for k in keys if not finished(k)]
        results: list[dict[str, Any]] = []
        stopped = ""
        for key in pending:
            if len(results) >= max_chats:
                stopped = "chat cap"
                break
            left = deadline - clock()
            if left <= 0:
                stopped = TIME_BUDGET
                break
            row = saved.get(key)
            res = sync_chat_history(
                bridge,
                key,
                target,
                max_pages=max_pages_per_chat,
                sleep=sleep,
                budget_seconds=left,
                clock=clock,
                empty_before=int(row["empty_pages"]) if row is not None and row["state"] == "pending" else 0,
                **kwargs,
            )
            conn.execute(
                "INSERT INTO history_sync (conv, back_to, pages, empty_pages, oldest, state, stopped, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (conv) DO UPDATE SET back_to = excluded.back_to, "
                "pages = history_sync.pages + excluded.pages, empty_pages = excluded.empty_pages, "
                "oldest = excluded.oldest, state = excluded.state, stopped = excluded.stopped, "
                "updated_at = excluded.updated_at",
                (
                    key,
                    target,
                    res.pages,
                    res.empty_pages,
                    res.oldest,
                    res.state,
                    res.stopped,
                    int(time.time()),
                ),
            )
            results.append(res.as_dict())
            if res.state != "pending":
                saved[key] = {"back_to": target, "empty_pages": res.empty_pages, "state": res.state}
            if res.stopped.startswith("bridge:") or res.stopped == TIME_BUDGET:
                stopped = res.stopped
                log.warning("history sync stopped: %s", res.stopped)
                break
    remaining = [k for k in pending if not finished(k)]
    out: dict[str, Any] = {
        "chats": results,
        "skipped_done": len(keys) - len(pending),
        "total_active": len(keys),
        "remaining": len(remaining),
        "complete": not remaining,
    }
    if remaining:
        out["stopped"] = stopped or "some chats need more pages"
        out["resume"] = "call sync_all_history again with the same arguments to continue"
    return out
