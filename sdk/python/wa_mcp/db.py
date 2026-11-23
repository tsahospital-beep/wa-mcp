"""Read access to the bridge database (store/wa.db) plus the derived_text writer.

The bridge owns the schema. This module opens wa.db read-only (mode=ro) for
every query; only put_derived opens it for writing. Sealed fields are opened
transparently when encryption is enabled. Text search is case-insensitive for
any script (str.casefold) and runs on the opened values, so it also works on
encrypted databases.

Rows are dicts with the schema column names plus "time" (ISO local time).
Media secrets (media_key, direct_path, cdn_url, hashes) are never returned.

Sealed values are opened only when the store has meta.crypt_salt and
encryption is enabled; otherwise a value that happens to start with "wae1:"
is plain text. A field that cannot be opened is returned unchanged.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from wa_mcp import config as config_mod
from wa_mcp import keystore, secretfile
from wa_mcp.crypt import CryptError, Sealer, aad, derive_key, field_key, is_sealed

log = logging.getLogger(__name__)

Row = dict[str, Any]
TimeArg = int | float | str | datetime | date | None

KNOWN_SOURCES = ("transcript", "document", "archive", "image", "link")
# Stored after the language code when the language was taken from the chat's history, not detected.
HINT_MARK = " hint"
# Message kinds in chat_messages.kind (bridge internal/store/kinds.go, plus ptv for round videos).
KINDS = (
    "text", "image", "video", "ptv", "audio", "voice", "document", "sticker",
    "location", "contact", "poll", "unsupported",
)  # fmt: skip
_SOURCE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")

_MESSAGE_COLUMNS = (
    "conv, msg_id, author, outgoing, sent_at, body, kind, attachment_name, mime, byte_size, "
    "local_path, reply_to_id, reply_to_author, reply_preview, mentioned, read_at"
)
# Newer bridge schemas add these; they are read when present.
_OPTIONAL_MESSAGE_COLUMNS = ("deleted_at", "edited_at", "deleted_unverified")
# Highest meta.min_reader_schema this reader understands (the bridge raises it when old readers would
# misread the store). 5: "wae2:" values with associated data, wa_contacts and wa_lid_map.
READER_SCHEMA = 5
# Message ids of chats loaded with `wa-bridge import-export` start with this.
IMPORTED_PREFIX = "imp-"
# meta entries whose change means the encryption state or the reader rules must be read again.
_STATE_SQL = (
    "SELECT key, value FROM meta WHERE key IN ('crypt_salt', 'crypt_check', 'crypt_kind', 'min_reader_schema') "
    "ORDER BY key"
)


class StoreError(Exception):
    """The database is missing, locked, or cannot be opened with this config."""


# Same rule as the bridge: integers above this are milliseconds.
MS_THRESHOLD = 100_000_000_000


def _seconds(n: int | float) -> int:
    try:
        whole = int(n)
    except (OverflowError, ValueError):
        raise ValueError("time must be finite") from None
    if abs(n) > MS_THRESHOLD:
        whole = (abs(whole) // 1000) * (-1 if whole < 0 else 1)
    if not -(1 << 63) <= whole < (1 << 63):
        raise ValueError("time is outside the SQLite integer range")
    return whole


def to_unix(value: TimeArg) -> int | None:
    """Unix seconds from int (milliseconds above MS_THRESHOLD), datetime, date (local midnight),
    or text (unix seconds or milliseconds, YYYY-MM-DD, ISO 8601)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("time must not be a boolean")
    if isinstance(value, int | float):
        return _seconds(value)
    bad = f"cannot read time {value!r}: use unix seconds, YYYY-MM-DD or ISO 8601"
    try:
        if isinstance(value, datetime):
            return int(value.timestamp())
        if isinstance(value, date):
            return int(datetime(value.year, value.month, value.day).timestamp())
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return _seconds(int(text))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return int(datetime.fromisoformat(text).timestamp())
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(bad) from exc


def iso_local(ts: int | None) -> str | None:
    """ISO local time; None for 0, None, or a value the platform cannot convert."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _as_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [v for v in value if v]


def _in(column: str, values: list[str]) -> str:
    return f"{column} IN ({', '.join('?' * len(values))})"


def _message_batches(keys: Sequence[tuple[str, str]]) -> Iterator[tuple[str, list[str]]]:
    unique = list(dict.fromkeys(keys))
    for start in range(0, len(unique), 400):
        batch = unique[start : start + 400]
        values = ",".join("(?,?)" for _ in batch)
        yield f"WITH wanted(conv,msg_id) AS (VALUES {values}) ", [v for pair in batch for v in pair]


@dataclass(frozen=True)
class Identities:
    """wa_contacts rows (jid and names), wa_lid_map pairs (full JIDs) and meta own_jid/own_lid."""

    contacts: list[Row]
    lid_pairs: list[tuple[str, str]]
    own: list[str]


class Store:
    """Access to one wa.db file."""

    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.path: Path = cfg.wa_db
        self._sealer: Sealer | None = None
        self._keys: dict[tuple[str, str], bytes] = {}
        self._ready = False
        self._stamp: tuple[int, tuple[Any, ...]] = (0, ())
        self._optional: tuple[str, ...] = ()
        self._has_reactions = False
        self._has_media_hash = False
        # One setup at a time; its results are assigned together at the end, so a reader in another thread
        # never sees a state without the sealer while a write or a check runs.
        self._setup_lock = threading.Lock()

    # connection handling

    @contextmanager
    def _ro(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise StoreError(f"no database at {self.path}: start the bridge once to create it")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=8, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open {self.path}: {exc}") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.create_function("wa_match", 2, self._match, deterministic=True)
            conn.create_function("wa_match_at", 7, self._match_at, deterministic=True)
            self._setup(conn)
            yield conn
        except sqlite3.Error as exc:
            raise StoreError(f"database error: {exc}") from exc
        finally:
            conn.close()

    def _setup(self, conn: sqlite3.Connection, force: bool = False) -> None:
        """Read the encryption state and optional columns. Every connection compares the schema cookie and
        the encryption entries of meta with the ones seen last, so a bridge that turns encryption on (or
        migrates the schema) while this process runs is noticed on the next query."""
        with self._setup_lock:
            stamp = (
                int(conn.execute("PRAGMA schema_version").fetchone()[0]),
                tuple(tuple(row) for row in conn.execute(_STATE_SQL)),
            )
            if self._ready and not force and stamp == self._stamp:
                return
            self._read_state(conn, stamp)

    def _read_state(self, conn: sqlite3.Connection, stamp: tuple[int, tuple[Any, ...]]) -> None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
        optional = tuple(c for c in _OPTIONAL_MESSAGE_COLUMNS if c in cols)
        has_reactions = bool(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reactions'").fetchone()
        )
        sealer: Sealer | None = None
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
        needed = str(meta.get("min_reader_schema") or "0")
        if not needed.isdigit() or int(needed) > READER_SCHEMA:
            raise StoreError(
                f"{self.path} was written by a newer bridge (it needs reader schema {needed}, this wa-mcp "
                f"package reads up to {READER_SCHEMA}): update the Python packages, for example with uv sync"
            )
        salt_text = meta.get("crypt_salt", "")
        if salt_text:
            if not self.cfg.encryption.enabled:
                raise StoreError(
                    "the database is encrypted, but encryption.mode is off: set encryption.mode to the mode "
                    "it was encrypted with (default keystore)"
                )
            # The kind recorded by the bridge decides where the key comes from (no kind: passphrase).
            kind = meta.get("crypt_kind") or "passphrase"
            try:
                salt = base64.b64decode(salt_text, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise StoreError("meta.crypt_salt is damaged") from exc
            try:
                sealer = Sealer(self._field_key(kind, salt_text, salt))
            except CryptError as exc:
                raise StoreError(str(exc)) from exc
            if not sealer.verify_check(meta.get("crypt_check", "")):
                if kind == "passphrase":
                    raise StoreError("wrong passphrase for this database")
                raise StoreError(f"the {kind} key does not open this database")
        self._optional, self._has_reactions, self._sealer = optional, has_reactions, sealer
        self._has_media_hash = "plain_sha256" in cols
        self._stamp = stamp
        self._ready = True

    def _field_key(self, kind: str, salt_text: str, salt: bytes) -> bytes:
        """The field key for the recorded kind; store keys are cached per salt for later writes."""
        enc = self.cfg.encryption
        if kind == "passphrase":
            try:
                passphrase = secretfile.passphrase(enc.passphrase_env)
            except secretfile.SecretFileError as exc:
                raise StoreError(f"{enc.passphrase_env}_FILE: {exc}") from exc
            if not passphrase:
                raise StoreError(
                    f"the database is encrypted with a passphrase, but the variable {enc.passphrase_env} "
                    f"(or {enc.passphrase_env}_FILE) is empty"
                )
            return derive_key(passphrase, salt)
        cached = self._keys.get((kind, salt_text))
        if cached is not None:
            return cached
        try:
            if kind == "keystore":
                secret = keystore.store_key(self.cfg.store_dir)
                if secret is None:
                    where = keystore.describe(self.cfg.store_dir, keystore.ITEM_STORE_KEY)
                    raise StoreError(
                        f"the database is encrypted with a key from the OS keystore, but {where} holds no key "
                        "for this store; it was copied from another computer or user, or the key was removed"
                    )
            elif kind == "keyfile":
                if not enc.key_file:
                    raise StoreError("the database is encrypted with a key file: set encryption.key_file")
                secret = keystore.read_key_file(enc.key_file)
            else:
                raise StoreError(f"unknown key kind {kind!r} in meta.crypt_kind")
        except keystore.KeystoreError as exc:
            raise StoreError(str(exc)) from exc
        key = field_key(secret, salt)
        self._keys[(kind, salt_text)] = key
        return key

    def _open(
        self,
        value: str | None,
        table: str = "",
        column: str = "",
        conv: str = "",
        msg_id: str = "",
        source: str | None = None,
    ) -> str:
        """Open a sealed field; a "wae2:" value also needs its place (table, column and the key columns)."""
        if not value:
            return ""
        if self._sealer is None or not is_sealed(value):
            return value
        try:
            return self._sealer.open_string(
                value, aad(table, column, conv, msg_id, source) if table else None
            )
        except CryptError:
            log.debug(
                "a sealed %s.%s value could not be opened; returned unchanged", table or "?", column or "?"
            )
            return value

    def draft_payload(self, draft_id: str, content: str, *, read: bool = False) -> str:
        """Seal or open a draft with this account's message-store field key."""
        with self._ro():
            place = aad("agent_drafts", "payload", draft_id)
            if read and is_sealed(content):
                if self._sealer is None:
                    raise StoreError("the draft key is unavailable")
                return self._sealer.open_string(content, place)
            if not read and self._sealer is not None:
                return self._sealer.seal_string(content, place)
            return content

    def _match(self, value: str | None, needle: str) -> int:
        """Search on columns that are never sealed (chat titles, JIDs)."""
        if not value:
            return 0
        return 1 if needle in self._open(value).casefold() else 0

    def _match_at(
        self,
        value: str | None,
        needle: str,
        table: str,
        column: str,
        conv: str,
        msg_id: str,
        source: str | None,
    ) -> int:
        """Search on a sealed column; the place is needed to open "wae2:" values."""
        if not value:
            return 0
        return 1 if needle in self._open(value, table, column, conv, msg_id, source).casefold() else 0

    @property
    def _columns(self) -> str:
        return ", ".join((_MESSAGE_COLUMNS, *self._optional))

    def _hide_deleted(self, include_deleted: bool) -> list[str]:
        return ["deleted_at IS NULL"] if "deleted_at" in self._optional and not include_deleted else []

    @property
    def encrypted(self) -> bool:
        with self._ro():
            return self._sealer is not None

    # rows

    def _message_row(self, r: sqlite3.Row) -> Row:
        row = dict(r)
        place = (str(row["conv"]), str(row["msg_id"]))
        row["body"] = self._open(row["body"], "chat_messages", "body", *place)
        row["attachment_name"] = (
            self._open(row["attachment_name"], "chat_messages", "attachment_name", *place) or None
        )
        row["reply_preview"] = (
            self._open(row["reply_preview"], "chat_messages", "reply_preview", *place) or None
        )
        row["outgoing"] = bool(row["outgoing"])
        mentioned = row.get("mentioned")
        try:
            row["mentioned"] = json.loads(mentioned) if mentioned else []
        except json.JSONDecodeError:
            row["mentioned"] = []
        row["time"] = iso_local(row["sent_at"])
        return row

    @staticmethod
    def _conversation_row(r: sqlite3.Row) -> Row:
        row = dict(r)
        row["is_group"] = bool(row["is_group"])
        row["time"] = iso_local(row["last_activity"])
        return row

    # conversations

    def conversations(self, query: str | None = None, limit: int | None = 50, offset: int = 0) -> list[Row]:
        stmt = "SELECT jid, title, is_group, last_activity FROM conversations"
        args: list[Any] = []
        needle = (query or "").strip().casefold()
        if needle:
            stmt += " WHERE wa_match(title, ?) OR wa_match(jid, ?)"
            args += [needle, needle]
        stmt += " ORDER BY last_activity DESC, jid ASC LIMIT ? OFFSET ?"
        args += [-1 if limit is None else max(limit, 0), max(offset, 0)]
        with self._ro() as conn:
            return [self._conversation_row(r) for r in conn.execute(stmt, args)]

    def conversation(self, jid: str) -> Row | None:
        with self._ro() as conn:
            r = conn.execute(
                "SELECT jid, title, is_group, last_activity FROM conversations WHERE jid = ?", (jid,)
            ).fetchone()
        return self._conversation_row(r) if r else None

    def active_chats(
        self, after: TimeArg = None, before: TimeArg = None, convs: Sequence[str] | None = None
    ) -> list[Row]:
        """Chats with at least one message in the period, most recent first."""
        lo, hi = to_unix(after), to_unix(before)
        where = ["1 = 1"]
        args: list[Any] = []
        if convs is not None:
            where.append(_in("m.conv", list(convs)))
            args.extend(convs)
        if lo is not None:
            where.append("m.sent_at > ?")
            args.append(lo)
        if hi is not None:
            where.append("m.sent_at < ?")
            args.append(hi)
        stmt = (
            "SELECT m.conv AS jid, COALESCE(c.title, '') AS title, COALESCE(c.is_group, 0) AS is_group, "
            "MAX(m.sent_at) AS last_activity, COUNT(*) AS message_count FROM chat_messages m "
            "LEFT JOIN conversations c ON c.jid = m.conv WHERE " + " AND ".join(where) + " "
            "GROUP BY m.conv ORDER BY last_activity DESC"
        )
        with self._ro() as conn:
            return [self._conversation_row(r) for r in conn.execute(stmt, args)]

    # messages

    def messages(
        self,
        conv: str | Sequence[str] | None = None,
        after: TimeArg = None,
        before: TimeArg = None,
        query: str | None = None,
        author: str | Sequence[str] | None = None,
        limit: int | None = 50,
        offset: int = 0,
        ascending: bool = False,
        kind: str | Sequence[str] | None = None,
        include_deleted: bool = False,
        include_imported: bool = True,
        outgoing: bool | None = None,
    ) -> list[Row]:
        """Messages newest first (or oldest first with ascending). limit=None means all.

        query matches body, attachment name and derived text (transcripts, extracted text).
        Messages deleted for everyone are left out unless include_deleted is set.
        """
        where: list[str] = []
        args: list[Any] = []
        convs, authors, kinds = _as_list(conv), _as_list(author), _as_list(kind)
        if outgoing is not None:
            where.append("outgoing = ?")
            args.append(outgoing)
        if not include_imported:
            where.append("msg_id NOT GLOB ?")
            args.append(IMPORTED_PREFIX + "*")
        if convs:
            where.append(_in("conv", convs))
            args += convs
        if authors:
            where.append(_in("author", authors))
            args += authors
        if kinds:
            where.append(_in("kind", kinds))
            args += kinds
        lo, hi = to_unix(after), to_unix(before)
        if lo is not None:
            where.append("sent_at > ?")
            args.append(lo)
        if hi is not None:
            where.append("sent_at < ?")
            args.append(hi)
        needle = (query or "").strip().casefold()
        if needle:
            where.append(
                "(wa_match_at(body, ?, 'chat_messages', 'body', conv, msg_id, NULL) "
                "OR wa_match_at(attachment_name, ?, 'chat_messages', 'attachment_name', conv, msg_id, NULL) "
                "OR EXISTS (SELECT 1 FROM derived_text d "
                "WHERE d.conv = chat_messages.conv AND d.msg_id = chat_messages.msg_id "
                "AND wa_match_at(d.content, ?, 'derived_text', 'content', d.conv, d.msg_id, d.source)))"
            )
            args += [needle, needle, needle]
        order = "ASC" if ascending else "DESC"
        args += [-1 if limit is None else max(limit, 0), max(offset, 0)]
        with self._ro() as conn:
            where += self._hide_deleted(include_deleted)
            stmt = f"SELECT {self._columns} FROM chat_messages"
            if where:
                stmt += " WHERE " + " AND ".join(where)
            stmt += f" ORDER BY sent_at {order}, msg_id {order} LIMIT ? OFFSET ?"
            return [self._message_row(r) for r in conn.execute(stmt, args)]

    def message(self, conv: str, msg_id: str) -> Row | None:
        with self._ro() as conn:
            r = conn.execute(
                f"SELECT {self._columns} FROM chat_messages WHERE conv = ? AND msg_id = ?", (conv, msg_id)
            ).fetchone()
            return self._message_row(r) if r else None

    def attachment_hash(self, conv: str, msg_id: str) -> bytes | None:
        """The bridge's SHA-256 for a downloaded attachment, when the store records one."""
        with self._ro() as conn:
            if self._has_media_hash:
                row = conn.execute(
                    "SELECT plain_sha256 FROM chat_messages WHERE conv=? AND msg_id=?", (conv, msg_id)
                ).fetchone()
                if row and isinstance(row[0], bytes) and len(row[0]) == 32:
                    return row[0]
        return None

    def find_message(self, msg_id: str) -> Row | None:
        """Look a message up by id alone (ids are unique in practice)."""
        with self._ro() as conn:
            r = conn.execute(
                f"SELECT {self._columns} FROM chat_messages WHERE msg_id = ? ORDER BY sent_at DESC LIMIT 1",
                (msg_id,),
            ).fetchone()
            return self._message_row(r) if r else None

    def context(
        self, conv: str, msg_id: str, before: int = 5, after: int = 5, include_deleted: bool = False
    ) -> Row | None:
        """{"before": [...], "message": row, "after": [...]}, oldest first; None if not found."""
        with self._ro() as conn:
            r = conn.execute(
                f"SELECT {self._columns} FROM chat_messages WHERE conv = ? AND msg_id = ?", (conv, msg_id)
            ).fetchone()
            if r is None or (
                not include_deleted and "deleted_at" in self._optional and r["deleted_at"] is not None
            ):
                return None
            t = r["sent_at"]
            hide = "".join(f" AND {w}" for w in self._hide_deleted(include_deleted))
            older = conn.execute(
                f"SELECT {self._columns} FROM chat_messages WHERE conv = ?{hide} "
                "AND (sent_at < ? OR (sent_at = ? AND msg_id < ?)) ORDER BY sent_at DESC, msg_id DESC LIMIT ?",
                (conv, t, t, msg_id, max(before, 0)),
            ).fetchall()
            newer = conn.execute(
                f"SELECT {self._columns} FROM chat_messages WHERE conv = ?{hide} "
                "AND (sent_at > ? OR (sent_at = ? AND msg_id > ?)) ORDER BY sent_at ASC, msg_id ASC LIMIT ?",
                (conv, t, t, msg_id, max(after, 0)),
            ).fetchall()
            return {
                "before": [self._message_row(x) for x in reversed(older)],
                "message": self._message_row(r),
                "after": [self._message_row(x) for x in newer],
            }

    # identities (copied by the bridge from its session, so readers never open session.db)

    def identities(self) -> Identities | None:
        """Contacts with their names opened, the LID map and the own addresses; None when the store
        comes from a bridge without wa_contacts (readers then fall back to session.db)."""
        with self._ro() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "wa_contacts" not in tables:
                return None
            contacts: list[Row] = []
            for r in conn.execute(
                "SELECT jid, first_name, full_name, push_name, business_name FROM wa_contacts"
            ):
                jid = str(r["jid"])
                contacts.append(
                    {"jid": jid}
                    | {
                        col: self._open(r[col], "wa_contacts", col, jid)
                        for col in ("first_name", "full_name", "push_name", "business_name")
                    }
                )
            pairs: list[tuple[str, str]] = []
            if "wa_lid_map" in tables:
                pairs = [
                    (str(r["lid"]), str(r["pn"])) for r in conn.execute("SELECT lid, pn FROM wa_lid_map")
                ]
            meta = dict(
                conn.execute("SELECT key, value FROM meta WHERE key IN ('own_jid', 'own_lid')").fetchall()
            )
        own = [str(meta[k]) for k in ("own_jid", "own_lid") if meta.get(k)]
        return Identities(contacts=contacts, lid_pairs=pairs, own=own)

    # derived text

    def _derived_row(self, r: sqlite3.Row) -> Row:
        row = dict(r)
        lang = row.get("lang") or ""
        row["lang_hinted"] = lang.endswith(HINT_MARK)
        if row["lang_hinted"]:
            row["lang"] = lang[: -len(HINT_MARK)] or None
        row["content"] = self._open(
            row["content"],
            "derived_text",
            "content",
            str(row["conv"]),
            str(row["msg_id"]),
            str(row["source"]),
        )
        row["time"] = iso_local(row["created_at"])
        return row

    def derived(self, conv: str, msg_id: str) -> list[Row]:
        with self._ro() as conn:
            rows = conn.execute(
                "SELECT conv, msg_id, source, lang, confidence, content, created_at FROM derived_text "
                "WHERE conv = ? AND msg_id = ? ORDER BY source",
                (conv, msg_id),
            ).fetchall()
            return [self._derived_row(r) for r in rows]

    def derived_many(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], list[Row]]:
        """Derived rows for many (conv, msg_id) pairs in one query."""
        if not keys:
            return {}
        with self._ro() as conn:
            return self._derived_many(conn, keys)

    def _derived_many(
        self, conn: sqlite3.Connection, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], list[Row]]:
        out: dict[tuple[str, str], list[Row]] = {}
        for prefix, args in _message_batches(keys):
            rows = conn.execute(
                prefix + "SELECT derived_text.* FROM wanted CROSS JOIN derived_text USING(conv,msg_id) "
                "ORDER BY source",
                args,
            )
            for r in rows:
                key = (r["conv"], r["msg_id"])
                out.setdefault(key, []).append(self._derived_row(r))
        return out

    def transcript_rows(
        self, convs: Sequence[str], after: TimeArg, before: TimeArg
    ) -> Iterator[tuple[Row, list[Row]]]:
        """Stream a chat and its derived text from the same SQLite snapshot."""
        with self._ro() as conn:
            conn.execute("BEGIN")
            where = [_in("conv", list(convs)), *self._hide_deleted(False)]
            args: list[Any] = list(convs)
            for operator, value in ((">", to_unix(after)), ("<", to_unix(before))):
                if value is not None:
                    where.append(f"sent_at {operator} ?")
                    args.append(value)
            cursor = conn.execute(
                f"SELECT {self._columns} FROM chat_messages WHERE "
                + " AND ".join(where)
                + " ORDER BY sent_at, msg_id, conv",
                args,
            )
            while batch := cursor.fetchmany(400):
                keys = [(r["conv"], r["msg_id"]) for r in batch]
                derived = self._derived_many(conn, keys)
                for row, key in zip(batch, keys, strict=True):
                    yield self._message_row(row), derived.get(key, [])

    def derived_by_source(self, source: str, conv: str | Sequence[str] | None = None) -> list[Row]:
        """All derived rows of one source, optionally limited to chats, newest first."""
        convs = _as_list(conv)
        stmt = "SELECT conv, msg_id, source, lang, confidence, content, created_at FROM derived_text WHERE source = ?"
        args: list[Any] = [source]
        if convs:
            stmt += " AND " + _in("conv", convs)
            args += convs
        stmt += " ORDER BY created_at DESC"
        with self._ro() as conn:
            return [self._derived_row(r) for r in conn.execute(stmt, args)]

    def reactions_many(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], list[Row]]:
        """Reactions (reactor, emoji as stored, reacted_at) for many (conv, msg_id) pairs, oldest first.

        Empty when the bridge schema has no reactions table yet.
        """
        out: dict[tuple[str, str], list[Row]] = {}
        if not keys:
            return out
        with self._ro() as conn:
            if not self._has_reactions:
                return out
            for prefix, args in _message_batches(keys):
                rows = conn.execute(
                    prefix + "SELECT reactions.conv, reactions.msg_id, reactor, emoji, reacted_at "
                    "FROM wanted CROSS JOIN reactions USING(conv,msg_id) ORDER BY reacted_at, reactor",
                    args,
                )
                for r in rows:
                    key = (r["conv"], r["msg_id"])
                    row = dict(r)
                    row["emoji"] = self._open(row["emoji"], "reactions", "emoji", *key)
                    out.setdefault(key, []).append(row)
        return out

    def derived_stats(self, conv: str | Sequence[str]) -> list[Row]:
        """Count and newest created_at of derived text per source; the text itself is not read."""
        convs = _as_list(conv)
        if not convs:
            return []
        with self._ro() as conn:
            rows = conn.execute(
                f"SELECT source, COUNT(*) AS count, MAX(created_at) AS newest FROM derived_text "
                f"WHERE {_in('conv', convs)} GROUP BY source ORDER BY source",
                convs,
            ).fetchall()
        return [{"source": r["source"], "count": r["count"], "newest": r["newest"] or 0} for r in rows]

    def put_derived(
        self,
        conv: str,
        msg_id: str,
        source: str,
        content: str,
        lang: str | None = None,
        confidence: float | None = None,
        hinted: bool = False,
    ) -> None:
        """Store text derived from a message (the only write Python makes to wa.db).

        hinted marks a language that came from the chat's history instead of detection; rows read back
        carry lang (the code only) and lang_hinted.

        The encryption state is read again right before the write, so text is never
        stored in the clear after the bridge turned encryption on.
        """
        if not _SOURCE_RE.match(source):
            raise ValueError(f"bad source name {source!r}")
        stored_lang = f"{lang}{HINT_MARK}" if lang and hinted else lang
        if not self.path.is_file():
            raise StoreError(f"no database at {self.path}: start the bridge once to create it")
        try:
            conn = sqlite3.connect(str(self.path), timeout=8)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open {self.path} for writing: {exc}") from exc
        try:
            conn.execute("PRAGMA busy_timeout = 8000")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                self._setup(conn, force=True)
                sealer = self._sealer
                salt = conn.execute("SELECT value FROM meta WHERE key = 'crypt_salt'").fetchone()
                if salt and salt[0] and sealer is None:
                    raise StoreError(
                        "the database is encrypted but its key is not ready; nothing was written"
                    )
                place = aad("derived_text", "content", conv, msg_id, source)
                stored = sealer.seal_string(content, place) if sealer is not None and content else content
                conn.execute(
                    "INSERT INTO derived_text (conv, msg_id, source, lang, confidence, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (conv, msg_id, source) DO UPDATE SET "
                    "lang = excluded.lang, confidence = excluded.confidence, content = excluded.content, "
                    "created_at = excluded.created_at",
                    (conv, msg_id, source, stored_lang, confidence, stored, int(time.time())),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot write derived text: {exc}") from exc
        finally:
            conn.close()

    # calls and schedules

    def calls(self, since: TimeArg = None, limit: int | None = 50, offset: int = 0) -> list[Row]:
        lo = to_unix(since) or 0
        with self._ro() as conn:
            rows = conn.execute(
                "SELECT call_id, peer, group_jid, outgoing, video, started_at, ended_at, outcome FROM call_log "
                "WHERE started_at >= ? ORDER BY started_at DESC, call_id LIMIT ? OFFSET ?",
                (lo, -1 if limit is None else max(limit, 0), max(0, offset)),
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["outgoing"], row["video"] = bool(row["outgoing"]), bool(row["video"])
            row["time"] = iso_local(row["started_at"])
            ended = row["ended_at"]
            row["duration_seconds"] = ended - row["started_at"] if ended else None
            out.append(row)
        return out

    def schedule_statuses(self, ids: Sequence[int]) -> dict[int, str]:
        statuses: dict[int, str] = {}
        with self._ro() as conn:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                params = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id, status FROM scheduled_messages WHERE id IN ({params})", batch
                )
                statuses.update((row["id"], row["status"]) for row in rows)
        return statuses

    def scheduled(self, status: str | None = None) -> list[Row]:
        return self.scheduled_page(status, None, 0)[0]

    def scheduled_page(self, status: str | None, limit: int | None, offset: int) -> tuple[list[Row], int]:
        table = " FROM scheduled_messages" + (" WHERE status = ?" if status else "")
        args = (status,) if status else ()
        stmt = "SELECT *" + table + " ORDER BY next_run ASC, id ASC LIMIT ? OFFSET ?"
        with self._ro() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(stmt, (*args, -1 if limit is None else max(0, limit), max(0, offset)))
            out = []
            for r in rows:
                row = dict(r)
                row["body"] = self._open(row["body"], "scheduled_messages", "body", str(row["target"]))
                try:
                    row["mentions"] = json.loads(row["mentions"]) if row.get("mentions") else []
                except json.JSONDecodeError:
                    row["mentions"] = []
                row["time"] = iso_local(row["next_run"])
                out.append(row)
            total = (
                len(out)
                if limit is None and offset <= 0
                else conn.execute("SELECT count(*)" + table, args).fetchone()[0]
            )
            return out, total


_default: Store | None = None
# Stores of the other accounts of the same config file, created on first use.
_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()
# The account that store() and the module functions work on; None = the account of connect().
_account: ContextVar[str | None] = ContextVar("wa_mcp_account", default=None)


def connect(cfg: config_mod.Config | None = None) -> Store:
    """Create the store for cfg (default: config.load()) and make it the module default."""
    global _default  # noqa: PLW0603
    _default = Store(cfg or config_mod.load())
    with _stores_lock:
        _stores.clear()
        _stores[_default.cfg.account] = _default
    return _default


def account() -> str:
    """Name of the account the module functions work on right now."""
    base = _default or connect()
    return _account.get() or base.cfg.account


@contextmanager
def use_account(name: str | None) -> Iterator[Store]:
    """Make the module functions of db and people work on another account of the same config
    (None = the account of connect()), for the current thread or task only."""
    found = store(name) if name else (_default or connect())
    token = _account.set(found.cfg.account)
    try:
        yield found
    finally:
        _account.reset(token)


def store(account: str | None = None) -> Store:
    """The store of an account (default: the current one, see use_account). An account that is not in
    the config raises config.ConfigError."""
    base = _default or connect()
    name = account or _account.get() or base.cfg.account
    if name == base.cfg.account:
        return base
    with _stores_lock:
        found = _stores.get(name)
        if found is None:
            found = _stores[name] = Store(base.cfg.for_account(name))
        return found


def conversations(query: str | None = None, limit: int | None = 50, offset: int = 0) -> list[Row]:
    return store().conversations(query=query, limit=limit, offset=offset)


def conversation(jid: str) -> Row | None:
    return store().conversation(jid)


def active_chats(
    after: TimeArg = None, before: TimeArg = None, convs: Sequence[str] | None = None
) -> list[Row]:
    return store().active_chats(after=after, before=before, convs=convs)


def messages(
    conv: str | Sequence[str] | None = None,
    after: TimeArg = None,
    before: TimeArg = None,
    query: str | None = None,
    author: str | Sequence[str] | None = None,
    limit: int | None = 50,
    offset: int = 0,
    ascending: bool = False,
    kind: str | Sequence[str] | None = None,
    include_deleted: bool = False,
    include_imported: bool = True,
    outgoing: bool | None = None,
) -> list[Row]:
    return store().messages(
        conv=conv,
        after=after,
        before=before,
        query=query,
        author=author,
        limit=limit,
        offset=offset,
        ascending=ascending,
        kind=kind,
        include_deleted=include_deleted,
        include_imported=include_imported,
        outgoing=outgoing,
    )


def message(conv: str, msg_id: str) -> Row | None:
    return store().message(conv, msg_id)


def find_message(msg_id: str) -> Row | None:
    return store().find_message(msg_id)


def context(
    conv: str, msg_id: str, before: int = 5, after: int = 5, include_deleted: bool = False
) -> Row | None:
    return store().context(conv, msg_id, before=before, after=after, include_deleted=include_deleted)


def derived(conv: str, msg_id: str) -> list[Row]:
    return store().derived(conv, msg_id)


def derived_many(keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], list[Row]]:
    return store().derived_many(keys)


def derived_by_source(source: str, conv: str | Sequence[str] | None = None) -> list[Row]:
    return store().derived_by_source(source, conv=conv)


def derived_stats(conv: str | Sequence[str]) -> list[Row]:
    return store().derived_stats(conv)


def reactions_many(keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], list[Row]]:
    return store().reactions_many(keys)


def put_derived(
    conv: str,
    msg_id: str,
    source: str,
    content: str,
    lang: str | None = None,
    confidence: float | None = None,
    hinted: bool = False,
) -> None:
    store().put_derived(conv, msg_id, source, content, lang=lang, confidence=confidence, hinted=hinted)


def calls(since: TimeArg = None, limit: int | None = 50, offset: int = 0) -> list[Row]:
    return store().calls(since=since, limit=limit, offset=offset)


def scheduled(status: str | None = None) -> list[Row]:
    return store().scheduled(status=status)


def identities() -> Identities | None:
    return store().identities()
