"""Python-owned insight state in store/insight.db: analytics cache and the task tracker.

All schema changes live in MIGRATIONS. Each one runs in its own BEGIN IMMEDIATE transaction and the
applied count (insight_meta key insight_schema) is re-read inside it, so two processes starting at once
do not apply a migration twice. History sync progress (wa_mcp.history) uses the same migrations.

When wa.db is encrypted, or its state cannot be read, no message text is kept here: on the first
connect in that state, copied text and the analytics cache are wiped with secure_delete on, and the
file is vacuumed so the old pages do not stay on disk. insight.db is created 0600 in a 0700 folder.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from wa_mcp import db, fsutil

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE IF NOT EXISTS analytics_cache (key TEXT PRIMARY KEY, chat TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, payload TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS analytics_cache_time ON analytics_cache(created_at)",
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, chat TEXT NOT NULL, chat_name TEXT NOT NULL DEFAULT '', "
        "is_group INTEGER NOT NULL DEFAULT 0, asker TEXT NOT NULL, asker_name TEXT NOT NULL DEFAULT '', "
        "category TEXT NOT NULL, score INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, "
        "status_why TEXT NOT NULL DEFAULT '', project TEXT, project_name TEXT, next_step TEXT NOT NULL DEFAULT '', "
        "due_at INTEGER, due_hint TEXT, first_conv TEXT NOT NULL, first_msg TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, pings INTEGER NOT NULL DEFAULT 0)",
        "CREATE INDEX IF NOT EXISTS tasks_chat ON tasks(chat, created_at)",
        "CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, updated_at)",
        "CREATE TABLE IF NOT EXISTS task_steps (task_id TEXT NOT NULL, conv TEXT NOT NULL, msg_id TEXT NOT NULL, "
        "at INTEGER NOT NULL, who TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', "
        "why TEXT NOT NULL DEFAULT '', PRIMARY KEY (task_id, conv, msg_id))",
        "CREATE INDEX IF NOT EXISTS task_steps_msg ON task_steps(msg_id)",
        "CREATE INDEX IF NOT EXISTS task_steps_at ON task_steps(at)",
    ),
    # History sync progress (wa_mcp.history). An older ad hoc table is replaced: it only held resumable
    # progress, so the next sync re-checks those chats. state: pending | reached | exhausted.
    (
        "DROP TABLE IF EXISTS history_sync",
        "CREATE TABLE history_sync (conv TEXT PRIMARY KEY, back_to INTEGER NOT NULL, pages INTEGER NOT NULL "
        "DEFAULT 0, empty_pages INTEGER NOT NULL DEFAULT 0, oldest INTEGER, state TEXT NOT NULL DEFAULT 'pending', "
        "stopped TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)",
    ),
    # Calls made to the optional AI provider per local day (wa_mcp.ai.usage). No message text.
    ("CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0)",),
    (
        "CREATE TABLE agent_drafts (id TEXT PRIMARY KEY, chat TEXT NOT NULL, kind TEXT NOT NULL, "
        "message_id TEXT NOT NULL, peer TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL, "
        "payload TEXT NOT NULL DEFAULT '', schedule_id INTEGER, audio_path TEXT, UNIQUE(chat,kind,message_id))",
        "CREATE INDEX agent_recent ON agent_drafts(peer,at)",
    ),
    ("CREATE INDEX agent_status_time ON agent_drafts(status,at)",),
)


def path() -> Path:
    return db.store().cfg.insight_db


def _version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM insight_meta WHERE key = 'insight_schema'").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply missing migrations; returns the schema version. conn must be in autocommit mode."""
    conn.execute("CREATE TABLE IF NOT EXISTS insight_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if _version(conn) >= len(MIGRATIONS):
        return len(MIGRATIONS)
    for number, statements in enumerate(MIGRATIONS, start=1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _version(conn) < number:
                for sql in statements:
                    conn.execute(sql)
                set_meta(conn, "insight_schema", str(number))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    return _version(conn)


def _encrypted() -> bool:
    """Fails closed: when wa.db cannot be read, act as if it were encrypted."""
    try:
        return db.store().encrypted
    except db.StoreError:
        return True


def _seal_existing(conn: sqlite3.Connection) -> None:
    """Drop copied message text once wa.db is encrypted; remember the mode so it runs once per switch."""
    mode = "sealed" if _encrypted() else "plain"
    if get_meta(conn, "text_mode") == mode:
        return
    if mode == "sealed":
        conn.execute("PRAGMA secure_delete = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if mode == "sealed":
            conn.execute("UPDATE tasks SET title = '', due_hint = NULL")
            conn.execute("UPDATE task_steps SET text = ''")
            conn.execute("DELETE FROM analytics_cache")
            conn.execute(
                "UPDATE agent_drafts SET status='skipped',payload='' WHERE payload <> '' "
                "AND payload NOT LIKE 'wae2:%'"
            )
        set_meta(conn, "text_mode", mode)
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    if mode == "sealed":
        conn.execute("VACUUM")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open insight.db (created on first use) with the schema up to date.

    The connection is in autocommit mode; use `with conn:` or explicit BEGIN for transactions.
    """
    target = path()
    fsutil.private_dir(target.parent)
    try:
        conn = sqlite3.connect(target, timeout=8, isolation_level=None)
    except sqlite3.Error as exc:
        raise db.StoreError(f"cannot open {target}: {exc}") from exc
    try:
        fsutil.restrict(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 8000")
        migrate(conn)
        _seal_existing(conn)
        yield conn
    except sqlite3.Error as exc:
        raise db.StoreError(f"insight database error: {exc}") from exc
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM insight_meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO insight_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def rekey_history(conn: sqlite3.Connection, key_of: Callable[[str], str]) -> int:
    """Move history_sync rows to the current key of each chat; when both forms exist the row that got
    further back in time is kept."""
    changed = 0
    for row in conn.execute("SELECT conv FROM history_sync").fetchall():
        conv = str(row[0])
        key = key_of(conv)
        if key == conv:
            continue
        other = conn.execute("SELECT oldest FROM history_sync WHERE conv = ?", (key,)).fetchone()
        mine = conn.execute("SELECT oldest FROM history_sync WHERE conv = ?", (conv,)).fetchone()
        if other is None:
            conn.execute("UPDATE history_sync SET conv = ? WHERE conv = ?", (key, conv))
        elif (mine[0] or 2**62) < (other[0] or 2**62):
            conn.execute("DELETE FROM history_sync WHERE conv = ?", (key,))
            conn.execute("UPDATE history_sync SET conv = ? WHERE conv = ?", (key, conv))
        else:
            conn.execute("DELETE FROM history_sync WHERE conv = ?", (conv,))
        changed += 1
    return changed


def stored_text(text: str, n: int = 300) -> str:
    """Text copied into insight.db; empty when wa.db is encrypted, so no plain copy leaves the sealed store."""
    if not text or _encrypted():
        return ""
    return text[:n]


def prune_cache(conn: sqlite3.Connection, days: float, keep_rows: int, now: float | None = None) -> None:
    """Drop analytics cache entries older than `days` and all but the newest keep_rows."""
    cutoff = int((time.time() if now is None else now) - days * 86400)
    conn.execute("DELETE FROM analytics_cache WHERE created_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM analytics_cache WHERE key NOT IN (SELECT key FROM analytics_cache ORDER BY created_at DESC LIMIT ?)",
        (keep_rows,),
    )
