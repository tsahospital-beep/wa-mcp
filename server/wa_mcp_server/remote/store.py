"""server.db in store_dir: static bearer tokens and OAuth state for remote mode.

Every secret (bearer tokens, OAuth client secrets, codes, access and refresh tokens, approval tickets,
consent cookies) is stored as an HMAC-SHA256 keyed with the server pepper. The plain value is shown once
and never written. The pepper comes from WA_MCP_SERVER_PEPPER (or its _FILE variant) or from
store/server.key, created on first use with mode 0600. No message text is stored here.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wa_mcp import fsutil
from wa_mcp.db import StoreError
from wa_mcp_server.remote.settings import secret_env

STATIC_PREFIX = "wam_"
ACCESS_PREFIX = "wao_"
REFRESH_PREFIX = "war_"
PEPPER_ENV = "WA_MCP_SERVER_PEPPER"
# Clients that registered but were never approved: at most this many, each kept this long.
PENDING_CLIENTS = 10
PENDING_CLIENT_SECONDS = 3600
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, "
    "created_at INTEGER NOT NULL, last_used_at INTEGER, revoked_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, "
    "secret_hash TEXT, created_at INTEGER NOT NULL, approved_at INTEGER, last_used_at INTEGER, "
    "revoked_at INTEGER, registered_ip TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS oauth_requests (id TEXT PRIMARY KEY, code TEXT NOT NULL, "
    "client_id TEXT NOT NULL, params TEXT NOT NULL, browser_hash TEXT, ticket_hash TEXT, "
    "status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS oauth_codes (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, "
    "data TEXT NOT NULL, grant_id TEXT, expires_at INTEGER NOT NULL, used_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS oauth_grants (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, "
    "scopes TEXT NOT NULL, resource TEXT NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS oauth_tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, "
    "grant_id TEXT NOT NULL, client_id TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER, "
    "created_at INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens (grant_id)",
)


def now() -> int:
    return int(time.time())


def new_secret(prefix: str = "") -> str:
    """256 random bits, URL-safe."""
    return prefix + secrets.token_urlsafe(32)


def load_pepper(store_dir: Path) -> bytes:
    """The HMAC key for stored secrets: WA_MCP_SERVER_PEPPER(_FILE), else store/server.key."""
    env = secret_env(PEPPER_ENV)
    if env:
        if len(env) < 32:
            raise StoreError(f"{PEPPER_ENV} must have at least 32 characters")
        return env.encode()
    path = store_dir / "server.key"
    fsutil.private_dir(store_dir)
    for _ in range(2):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc.strerror}") from exc
        if len(text) >= 32:
            return text.encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write((secrets.token_hex(32) + "\n").encode())
        fsutil.restrict(path)
    raise StoreError(f"{path} is empty or too short; delete it to create a new pepper")


@dataclass(frozen=True)
class TokenInfo:
    id: str
    name: str
    created_at: int
    last_used_at: int | None
    revoked_at: int | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class ClientRecord:
    client_id: str
    info: dict[str, Any]
    secret_hash: str | None
    created_at: int
    approved_at: int | None
    last_used_at: int | None
    revoked_at: int | None
    registered_ip: str = ""


@dataclass(frozen=True)
class RequestRecord:
    id: str
    code: str
    client_id: str
    params: dict[str, Any]
    browser_hash: str | None
    ticket_hash: str | None
    status: str
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class CodeRecord:
    client_id: str
    data: dict[str, Any]
    grant_id: str | None
    expires_at: int
    used_at: int | None


@dataclass(frozen=True)
class OAuthTokenRecord:
    kind: str
    grant_id: str
    client_id: str
    expires_at: int
    used_at: int | None
    scopes: tuple[str, ...]
    resource: str
    revoked: bool


@dataclass(frozen=True)
class Issued:
    """Hashes and lifetimes of a new access and refresh token pair."""

    grant_id: str
    access_hash: str
    access_expires: int
    refresh_hash: str
    refresh_expires: int


class ServerStore:
    def __init__(self, path: Path, pepper: bytes) -> None:
        self.path = path
        self._pepper = pepper
        self._ready = False

    @classmethod
    def open(cls, store_dir: Path) -> ServerStore:
        return cls(store_dir / "server.db", load_pepper(store_dir))

    def hash(self, secret: str) -> str:
        return hmac.new(self._pepper, secret.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, secret: str, digest: str | None) -> bool:
        return digest is not None and hmac.compare_digest(self.hash(secret), digest)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        fsutil.private_dir(self.path.parent)
        try:
            conn = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open {self.path}: {exc}") from exc
        try:
            fsutil.restrict(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 8000")
            if not self._ready:
                conn.execute("PRAGMA journal_mode = WAL")
                for stmt in SCHEMA:
                    conn.execute(stmt)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(oauth_clients)")}
                if "registered_ip" not in cols:
                    conn.execute(
                        "ALTER TABLE oauth_clients ADD COLUMN registered_ip TEXT NOT NULL DEFAULT ''"
                    )
                self._ready = True
            yield conn
        except sqlite3.Error as exc:
            raise StoreError(f"server database error: {exc}") from exc
        finally:
            conn.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # static tokens

    def create_token(self, name: str) -> tuple[TokenInfo, str]:
        name = " ".join(name.split())
        if not _NAME.match(name):
            raise ValueError("token name: 1 to 64 letters, digits, spaces, dots, dashes or underscores")
        secret = new_secret(STATIC_PREFIX)
        info = TokenInfo(
            id="tok_" + secrets.token_hex(4), name=name, created_at=now(), last_used_at=None, revoked_at=None
        )
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM tokens WHERE name = ? AND revoked_at IS NULL", (name,)).fetchone():
                raise ValueError(
                    f"an active token named {name!r} exists; revoke it first or pick another name"
                )
            conn.execute(
                "INSERT INTO tokens (id, name, hash, created_at) VALUES (?, ?, ?, ?)",
                (info.id, name, self.hash(secret), info.created_at),
            )
        return info, secret

    def list_tokens(self) -> list[TokenInfo]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM tokens ORDER BY created_at, id").fetchall()
        return [_token(r) for r in rows]

    def active_token_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM tokens WHERE revoked_at IS NULL").fetchone()
        return int(row[0])

    def revoke_token(self, ident: str) -> TokenInfo | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE (id = ? OR name = ?) AND revoked_at IS NULL", (ident, ident)
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE tokens SET revoked_at = ? WHERE id = ?", (now(), row["id"]))
        return _token(row)

    def check_token(self, secret: str) -> TokenInfo | None:
        """The active token with this value; last_used_at is refreshed at most once a minute."""
        if not secret.startswith(STATIC_PREFIX):
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE hash = ? AND revoked_at IS NULL", (self.hash(secret),)
            ).fetchone()
            if row is None:
                return None
            t = now()
            if row["last_used_at"] is None or t - int(row["last_used_at"]) >= 60:
                conn.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (t, row["id"]))
        return _token(row)

    # OAuth clients

    def add_client(
        self, client_id: str, info: dict[str, Any], secret: str | None, ip: str, max_approved: int
    ) -> None:
        """Store a registered client. Refused while `max_approved` approved clients exist. Clients that were
        never approved count separately: they expire after PENDING_CLIENT_SECONDS, and beyond
        PENDING_CLIENTS the oldest of them is dropped."""
        t = now()
        with self._tx() as conn:
            self._drop_pending(conn, "created_at < ?", (t - PENDING_CLIENT_SECONDS,))
            approved = conn.execute(
                "SELECT COUNT(*) FROM oauth_clients WHERE revoked_at IS NULL AND approved_at IS NOT NULL"
            ).fetchone()[0]
            if int(approved) >= max_approved:
                raise ValueError(
                    "too many approved clients (server.oauth.max_clients): remove one with "
                    "wa-mcp oauth revoke CLIENT_ID"
                )
            pending = [
                r[0]
                for r in conn.execute(
                    "SELECT client_id FROM oauth_clients WHERE approved_at IS NULL AND revoked_at IS NULL "
                    "ORDER BY created_at, rowid"
                )
            ]
            for old in pending[: max(0, len(pending) - PENDING_CLIENTS + 1)]:
                self._drop_pending(conn, "client_id = ?", (old,))
            conn.execute(
                "INSERT INTO oauth_clients (client_id, info, secret_hash, created_at, registered_ip) "
                "VALUES (?, ?, ?, ?, ?)",
                (client_id, json.dumps(info), self.hash(secret) if secret else None, t, ip),
            )

    @staticmethod
    def _drop_pending(conn: sqlite3.Connection, where: str, args: tuple[Any, ...]) -> None:
        """Delete never-approved clients matching `where` and their waiting requests."""
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT client_id FROM oauth_clients WHERE approved_at IS NULL AND {where}", args
            )
        ]
        for cid in ids:
            conn.execute("DELETE FROM oauth_requests WHERE client_id = ?", (cid,))
            conn.execute("DELETE FROM oauth_clients WHERE client_id = ?", (cid,))

    def approve_client(self, client_id: str) -> None:
        """The owner approved a request of this client: it now counts toward server.oauth.max_clients."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE oauth_clients SET approved_at = COALESCE(approved_at, ?) WHERE client_id = ?",
                (now(), client_id),
            )

    def get_client(self, client_id: str) -> ClientRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return _client(row) if row else None

    def list_clients(self) -> list[ClientRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM oauth_clients ORDER BY created_at").fetchall()
        return [_client(r) for r in rows]

    def revoke_client(self, client_id: str) -> bool:
        t = now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE oauth_clients SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                (t, client_id),
            )
            conn.execute(
                "UPDATE oauth_grants SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                (t, client_id),
            )
            conn.execute(
                "UPDATE oauth_requests SET status = 'denied' WHERE client_id = ? AND status = 'pending'",
                (client_id,),
            )
        return cur.rowcount > 0

    # authorization requests (consent)

    def add_request(self, rec: RequestRecord) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM oauth_requests WHERE expires_at < ?", (now() - 3600,))
            conn.execute(
                "INSERT INTO oauth_requests (id, code, client_id, params, browser_hash, ticket_hash, status, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.id,
                    rec.code,
                    rec.client_id,
                    json.dumps(rec.params),
                    rec.browser_hash,
                    rec.ticket_hash,
                    rec.status,
                    rec.created_at,
                    rec.expires_at,
                ),
            )

    def get_request(self, request_id: str) -> RequestRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM oauth_requests WHERE id = ?", (request_id,)).fetchone()
        return _request(row) if row else None

    def request_by_ticket(self, ticket_hash: str) -> RequestRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_requests WHERE ticket_hash = ?", (ticket_hash,)
            ).fetchone()
        return _request(row) if row else None

    def pending_requests(self) -> list[RequestRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM oauth_requests WHERE status = 'pending' AND expires_at >= ? ORDER BY created_at",
                (now(),),
            ).fetchall()
        return [_request(r) for r in rows]

    def bind_browser(self, request_id: str, browser_hash: str) -> bool:
        """The first browser that opens the consent page owns the request."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE oauth_requests SET browser_hash = ? WHERE id = ? AND browser_hash IS NULL",
                (browser_hash, request_id),
            )
        return cur.rowcount == 1

    def set_ticket(self, request_id: str, ticket_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE oauth_requests SET ticket_hash = ? WHERE id = ?", (ticket_hash, request_id))

    def move_request(self, request_id: str, old: str, new: str) -> bool:
        """Change the status atomically; False when it was not `old` (or the request expired)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE oauth_requests SET status = ?, ticket_hash = NULL "
                "WHERE id = ? AND status = ? AND expires_at >= ?",
                (new, request_id, old, now()),
            )
        return cur.rowcount == 1

    # codes, grants, tokens

    def add_code(self, code_hash: str, client_id: str, data: dict[str, Any], expires_at: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now() - 86400,))
            conn.execute(
                "INSERT INTO oauth_codes (hash, client_id, data, expires_at) VALUES (?, ?, ?, ?)",
                (code_hash, client_id, json.dumps(data), expires_at),
            )

    def get_code(self, code_hash: str) -> CodeRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE hash = ?", (code_hash,)).fetchone()
        if row is None:
            return None
        return CodeRecord(
            client_id=row["client_id"],
            data=json.loads(row["data"]),
            grant_id=row["grant_id"],
            expires_at=int(row["expires_at"]),
            used_at=row["used_at"],
        )

    def exchange_code(
        self, code_hash: str, client_id: str, scopes: list[str], resource: str, issued: Issued
    ) -> bool:
        """Spend the code and start a grant with its first token pair, in one transaction."""
        t = now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE oauth_codes SET used_at = ?, grant_id = ? "
                "WHERE hash = ? AND client_id = ? AND used_at IS NULL AND expires_at >= ?",
                (t, issued.grant_id, code_hash, client_id, t),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO oauth_grants (id, client_id, scopes, resource, created_at) VALUES (?, ?, ?, ?, ?)",
                (issued.grant_id, client_id, " ".join(scopes), resource, t),
            )
            self._insert_pair(conn, client_id, issued, t)
            conn.execute(
                "UPDATE oauth_clients SET approved_at = COALESCE(approved_at, ?), last_used_at = ? "
                "WHERE client_id = ?",
                (t, t, client_id),
            )
        return True

    def rotate_refresh(self, refresh_hash: str, client_id: str, issued: Issued) -> bool:
        """Spend the refresh token and add a new pair to the same grant; False if it was used already."""
        t = now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE oauth_tokens SET used_at = ? WHERE hash = ? AND kind = 'refresh' AND client_id = ? "
                "AND grant_id = ? AND used_at IS NULL AND expires_at >= ?",
                (t, refresh_hash, client_id, issued.grant_id, t),
            )
            if cur.rowcount != 1:
                return False
            revoked = conn.execute(
                "SELECT revoked_at FROM oauth_grants WHERE id = ?", (issued.grant_id,)
            ).fetchone()
            if revoked is None or revoked[0] is not None:
                return False
            self._insert_pair(conn, client_id, issued, t)
            conn.execute("UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?", (t, client_id))
        return True

    @staticmethod
    def _insert_pair(conn: sqlite3.Connection, client_id: str, issued: Issued, t: int) -> None:
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (t,))
        conn.executemany(
            "INSERT INTO oauth_tokens (hash, kind, grant_id, client_id, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (issued.access_hash, "access", issued.grant_id, client_id, issued.access_expires, t),
                (issued.refresh_hash, "refresh", issued.grant_id, client_id, issued.refresh_expires, t),
            ],
        )

    def get_token(self, token_hash: str) -> OAuthTokenRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT t.kind, t.grant_id, t.client_id, t.expires_at, t.used_at, g.scopes, g.resource, "
                "g.revoked_at AS grant_revoked, c.revoked_at AS client_revoked FROM oauth_tokens t "
                "JOIN oauth_grants g ON g.id = t.grant_id JOIN oauth_clients c ON c.client_id = t.client_id "
                "WHERE t.hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        return OAuthTokenRecord(
            kind=row["kind"],
            grant_id=row["grant_id"],
            client_id=row["client_id"],
            expires_at=int(row["expires_at"]),
            used_at=row["used_at"],
            scopes=tuple(str(row["scopes"]).split()),
            resource=row["resource"],
            revoked=row["grant_revoked"] is not None or row["client_revoked"] is not None,
        )

    def revoke_grant(self, grant_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE oauth_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now(), grant_id),
            )

    def active_grants(self, client_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM oauth_grants WHERE client_id = ? AND revoked_at IS NULL", (client_id,)
            ).fetchone()
        return int(row[0])


def _token(row: sqlite3.Row) -> TokenInfo:
    return TokenInfo(
        id=row["id"],
        name=row["name"],
        created_at=int(row["created_at"]),
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def _client(row: sqlite3.Row) -> ClientRecord:
    return ClientRecord(
        client_id=row["client_id"],
        info=json.loads(row["info"]),
        secret_hash=row["secret_hash"],
        created_at=int(row["created_at"]),
        approved_at=row["approved_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
        registered_ip=row["registered_ip"] or "",
    )


def _request(row: sqlite3.Row) -> RequestRecord:
    return RequestRecord(
        id=row["id"],
        code=row["code"],
        client_id=row["client_id"],
        params=json.loads(row["params"]),
        browser_hash=row["browser_hash"],
        ticket_hash=row["ticket_hash"],
        status=row["status"],
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
    )
