"""Names and identities that the bridge copies into wa.db (wa_contacts, wa_lid_map, meta own_jid/own_lid).

The bridge copies them from its whatsmeow session, so this module never needs store/session.db, which
holds the keys of the linked device. Only a store from an older bridge (without wa_contacts) is read
from session.db, with a warning.

WhatsApp addresses one person by a phone JID (<digits>@s.whatsapp.net) or a
LID (<digits>@lid). The LID map links the two (kept here as user parts), so
same_person() returns both forms. Names come from config aliases first, then
the address book, business and push names stored by whatsmeow, then chat titles.
Push names and chat titles are chosen by other people; name_info() says which
source a name came from, and send targets never resolve through them.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wa_mcp import config as config_mod
from wa_mcp import db

PHONE_SERVER = "s.whatsapp.net"
LID_SERVER = "lid"
GROUP_SERVER = "g.us"
log = logging.getLogger(__name__)

_RELOAD_SECONDS = 5.0
# Same rule as the bridge (internal/wa/jid.go): 7 to 15 digits, no leading 0.
_PHONE_RE = re.compile(r"\+?[\d\s().-]+")


class AmbiguousChat(LookupError):
    """A chat name matched several chats; candidates lists them."""

    def __init__(self, text: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(f"{text!r} matches {len(candidates)} chats; use a JID from the candidates")
        self.candidates = candidates


def bare(jid: str) -> str:
    """Drop the device and agent parts: 15550100001.0:12@s.whatsapp.net -> 15550100001@s.whatsapp.net."""
    jid = jid.strip()
    if "@" not in jid:
        return jid
    user, server = jid.split("@", 1)
    user = user.split(":", 1)[0]
    if server in (PHONE_SERVER, LID_SERVER):
        user = user.split(".", 1)[0]
    return f"{user}@{server}"


def user_of(jid: str) -> str:
    return bare(jid).split("@", 1)[0]


def server_of(jid: str) -> str:
    return bare(jid).split("@", 1)[1] if "@" in jid else ""


def phone_jid(text: str) -> str | None:
    """'+1 555 0100-002' -> '15550100002@s.whatsapp.net'; None if text is not a phone number."""
    if not _PHONE_RE.fullmatch(text.strip()):
        return None
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return f"{digits}@{PHONE_SERVER}"


@dataclass
class _Data:
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    lid_to_pn: dict[str, str] = field(default_factory=dict)
    pn_to_lid: dict[str, str] = field(default_factory=dict)
    mine: list[str] = field(default_factory=list)
    stamp: tuple[int, ...] | None = None
    loaded_at: float = 0.0


class People:
    """Name and identity lookups for one config."""

    def __init__(self, cfg: config_mod.Config, store: db.Store | None = None) -> None:
        self.cfg = cfg
        self.path: Path = cfg.session_db
        self._store = store
        self._data = _Data()
        self._warned = False
        self._aliases = {self._alias_key(k): v for k, v in cfg.aliases.items() if v.strip()}

    @property
    def store(self) -> db.Store:
        return self._store or db.store()

    @staticmethod
    def _alias_key(key: str) -> str:
        key = key.strip()
        if "@" in key:
            return bare(key)
        pj = phone_jid(key)
        return pj or key

    def _stamp(self) -> tuple[int, ...]:
        """Size and mtime of wa.db, session.db and their -wal files; writes land in the WAL first."""
        out: list[int] = []
        files = (self.store.path, self.path)
        for p in [q for f in files for q in (f, f.with_name(f.name + "-wal"))]:
            try:
                st = p.stat()
                out += [st.st_mtime_ns, st.st_size]
            except OSError:
                out += [0, 0]
        return tuple(out)

    def _load(self) -> _Data:
        now = time.monotonic()
        data = self._data
        if data.stamp is not None and now - data.loaded_at < _RELOAD_SECONDS:
            return data
        stamp = self._stamp()
        if stamp == data.stamp:
            data.loaded_at = now
            return data
        fresh = _Data(stamp=stamp, loaded_at=now)
        if (stamp[0] or stamp[4]) and not self._read(fresh):
            # Not cached: a failed or partial read is retried on the next lookup.
            return _Data()
        self._data = fresh
        return fresh

    def _read(self, out: _Data) -> bool:
        try:
            ids = self.store.identities() if self.store.path.is_file() else None
        except db.StoreError as exc:
            log.warning("cannot read names from %s: %s", self.store.path, exc)
            return False
        if ids is not None:
            for c in ids.contacts:
                entry = out.names.setdefault(bare(str(c["jid"])), {})
                for key, col in (("first", "first_name"), ("full", "full_name"), ("push", "push_name"),
                                 ("business", "business_name")):  # fmt: skip
                    value = str(c.get(col) or "").strip()
                    if value:
                        entry[key] = value
            for lid, pn in ids.lid_pairs:
                if lid and pn:
                    out.lid_to_pn[user_of(lid)] = user_of(pn)
                    out.pn_to_lid[user_of(pn)] = user_of(lid)
            for jid in ids.own:
                if bare(jid) not in out.mine:
                    out.mine.append(bare(jid))
            return True
        if not self.path.is_file():
            return True
        if not self._warned:
            log.warning(
                "names are read from %s because wa.db has no copy of them yet; start a current bridge once "
                "(reading the session file is deprecated)",
                self.path,
            )
            self._warned = True
        return self._read_session(out)

    def _read_session(self, out: _Data) -> bool:
        """Older stores only: names and the LID map from whatsmeow's own tables."""
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as exc:
            log.warning("cannot open %s: %s", self.path, exc)
            return False
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "whatsmeow_contacts" in tables:
                rows = conn.execute(
                    "SELECT their_jid, first_name, full_name, push_name, business_name FROM whatsmeow_contacts"
                )
                for their, first, full, push, business in rows:
                    if not their:
                        continue
                    entry = out.names.setdefault(bare(their), {})
                    for key, value in (
                        ("first", first),
                        ("full", full),
                        ("push", push),
                        ("business", business),
                    ):
                        if value and value.strip():
                            entry[key] = value.strip()
            if "whatsmeow_lid_map" in tables:
                for lid, pn in conn.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                    if lid and pn:
                        out.lid_to_pn[lid] = pn
                        out.pn_to_lid[pn] = lid
            if "whatsmeow_device" in tables:
                for jid, lid in conn.execute("SELECT jid, lid FROM whatsmeow_device"):
                    for value in (jid, lid):
                        if value and bare(value) not in out.mine:
                            out.mine.append(bare(value))
        except sqlite3.Error as exc:
            log.warning("cannot read names from %s: %s", self.path, exc)
            return False
        finally:
            conn.close()
        return True

    # identity

    def same_person(self, jid: str) -> list[str]:
        """All JIDs of the person behind jid (phone form first). Groups return [jid]."""
        j = bare(jid)
        if "@" not in j:
            pj = phone_jid(j)
            if pj is None:
                return [j]
            j = pj
        user, server = j.split("@", 1)
        data = self._load()
        if server == LID_SERVER:
            pn = data.lid_to_pn.get(user)
            return [f"{pn}@{PHONE_SERVER}", j] if pn else [j]
        if server == PHONE_SERVER:
            lid = data.pn_to_lid.get(user)
            return [j, f"{lid}@{LID_SERVER}"] if lid else [j]
        return [j]

    def my_jids(self) -> list[str]:
        """Our own phone JID and LID, without device parts."""
        mine = list(self._load().mine)
        for j in list(mine):
            for other in self.same_person(j):
                if other not in mine:
                    mine.append(other)
        return mine

    def phone_of(self, jid: str) -> str | None:
        for j in self.same_person(jid):
            if server_of(j) == PHONE_SERVER:
                return "+" + user_of(j)
        return None

    # names

    def name_info(self, jid: str) -> tuple[str, str]:
        """(name, source). Source: alias, contact (address book), business, push (chosen by the
        person), chat_title (chosen by the person or group admins), phone or jid."""
        if not jid:
            return "", "jid"
        jids = self.same_person(jid)
        for j in jids:
            alias = self._aliases.get(j)
            if alias:
                return alias, "alias"
        data = self._load()
        order = (("full", "contact"), ("first", "contact"), ("business", "business"), ("push", "push"))
        for key, source in order:
            for j in jids:
                value = data.names.get(j, {}).get(key)
                if value:
                    return value, source
        with contextlib.suppress(db.StoreError):
            for j in jids:
                conv = self.store.conversation(j)
                if conv and conv["title"]:
                    return str(conv["title"]), "chat_title"
        phone = self.phone_of(jid)
        return (phone, "phone") if phone else (bare(jid), "jid")

    def name_of(self, jid: str) -> str:
        """Best display name: alias, contact name, push name, chat title, phone number, then the JID."""
        return self.name_info(jid)[0]

    def resolve_recipient(self, text: str) -> list[str]:
        """JIDs of a send target given as JID, phone number, configured alias, or the exact
        address-book name (full or first). Push names and chat titles are never used.

        Raises LookupError when nothing matches and AmbiguousChat when several people match.
        """
        text = text.strip()
        if not text:
            raise LookupError("empty recipient")
        if "@" in text:
            return self.same_person(text)
        pj = phone_jid(text)
        if pj:
            return self.same_person(pj)
        needle = text.casefold()
        found: dict[str, list[str]] = {}
        for key, alias in self._aliases.items():
            if alias.casefold() == needle and "@" in key:
                jids = self.same_person(key)
                found.setdefault(jids[0], jids)
        if not found:
            for jid, names in self._load().names.items():
                if any(names.get(k, "").casefold() == needle for k in ("full", "first")):
                    jids = self.same_person(jid)
                    found.setdefault(jids[0], jids)
        if not found:
            raise LookupError(
                f"no contact named exactly {text!r}: use a JID, a phone number with country code, "
                "an address-book name or a configured alias"
            )
        if len(found) > 1:
            raise AmbiguousChat(text, [{"name": self.name_of(k), "jid": k} for k in found][:20])
        return next(iter(found.values()))

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """People whose name, alias or number contains query (casefolded)."""
        needle = query.strip().casefold()
        digits = re.sub(r"\D", "", query)
        if not needle:
            return []
        data = self._load()
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        candidates = list(data.names) + list(self._aliases)
        with contextlib.suppress(db.StoreError):
            candidates += [c["jid"] for c in self.store.conversations(query=query, limit=200)]
        for jid in candidates:
            if "@" not in jid or server_of(jid) == GROUP_SERVER:
                continue
            jids = self.same_person(jid)
            if jids[0] in seen:
                continue
            names = [self._aliases.get(j, "") for j in jids]
            names += [v for j in jids for v in data.names.get(j, {}).values()]
            numbers = [user_of(j) for j in jids if server_of(j) == PHONE_SERVER]
            hit = any(needle in n.casefold() for n in names if n) or bool(
                digits and len(digits) >= 3 and any(digits in n for n in numbers)
            )
            if not hit:
                try:
                    hit = any(
                        needle in (c["title"] or "").casefold()
                        for j in jids
                        if (c := self.store.conversation(j)) is not None
                    )
                except db.StoreError:
                    hit = False
            if not hit:
                continue
            seen.add(jids[0])
            name, source = self.name_info(jids[0])
            out.append({"name": name, "name_source": source, "jids": jids, "phone": self.phone_of(jids[0])})
            if len(out) >= limit:
                break
        return out

    def resolve_chat(self, text: str) -> list[str]:
        """Turn a JID, phone number or chat name into the JIDs of that chat (LID and phone merged).

        Raises LookupError when nothing matches and AmbiguousChat when several chats match.
        """
        text = text.strip()
        if not text:
            raise LookupError("empty chat")
        if "@" in text:
            return self.same_person(text)
        pj = phone_jid(text)
        if pj:
            return self.same_person(pj)
        needle = text.casefold()
        found: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(db.StoreError):
            for c in self.store.conversations(query=text, limit=50):
                jids = self.same_person(c["jid"])
                found.setdefault(jids[0], {"jids": jids, "name": c["title"] or self.name_of(c["jid"])})
        for p in self.search(text, limit=50):
            found.setdefault(p["jids"][0], {"jids": p["jids"], "name": p["name"]})
        if not found:
            raise LookupError(f"no chat or contact matches {text!r}")
        if len(found) == 1:
            return list(next(iter(found.values()))["jids"])
        exact = [v for v in found.values() if str(v["name"]).casefold() == needle]
        if len(exact) == 1:
            return list(exact[0]["jids"])
        raise AmbiguousChat(text, [{"name": v["name"], "jid": v["jids"][0]} for v in found.values()][:20])


_default: People | None = None
# People of the other accounts of the same config file, created on first use.
_others: dict[str, People] = {}
_others_lock = threading.Lock()


def connect(cfg: config_mod.Config | None = None, store: db.Store | None = None) -> People:
    global _default  # noqa: PLW0603
    _default = People(cfg or (store.cfg if store else db.store().cfg), store=store)
    with _others_lock:
        _others.clear()
    return _default


def people(account: str | None = None) -> People:
    """Lookups of an account (default: the current one, see db.use_account)."""
    base = _default or connect()
    name = account or db.account()
    if name == base.cfg.account:
        return base
    with _others_lock:
        found = _others.get(name)
        if found is None:
            st = db.store(name)
            found = _others[name] = People(st.cfg, store=st)
        return found


def same_person(jid: str) -> list[str]:
    return people().same_person(jid)


def name_of(jid: str) -> str:
    return people().name_of(jid)


def name_info(jid: str) -> tuple[str, str]:
    return people().name_info(jid)


def resolve_recipient(text: str) -> list[str]:
    return people().resolve_recipient(text)


def my_jids() -> list[str]:
    return people().my_jids()


def phone_of(jid: str) -> str | None:
    return people().phone_of(jid)


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    return people().search(query, limit=limit)


def resolve_chat(text: str) -> list[str]:
    return people().resolve_chat(text)
