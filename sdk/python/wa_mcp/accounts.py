"""Several WhatsApp accounts in one process.

config.json may list more numbers under "accounts". Each account has its own bridge (own api.listen
and token) and its own store folder with wa.db, session.db and insight.db. handles() returns the
config, database, names and bridge client of one account. use() makes the module functions of db and
people, and everything built on them such as the insight modules, work on one account in the current
thread or task. Without an "accounts" block there is one account, "default", and nothing changes.

    from wa_mcp import accounts, config

    accounts.connect(config.load("config.json"))
    for name in accounts.names():
        with accounts.use(name):
            ...
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wa_mcp import config as config_mod
from wa_mcp import db, people
from wa_mcp.client import Bridge, BridgeError

DEFAULT = config_mod.DEFAULT_ACCOUNT

_bridges: dict[str, Bridge] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Handles:
    """Everything that belongs to one account."""

    name: str
    cfg: config_mod.Config
    store: db.Store
    people: people.People

    @property
    def bridge(self) -> Bridge:
        """Client for the bridge of this account (created once, closed by close())."""
        return bridge(self.name)


def connect(cfg: config_mod.Config) -> db.Store:
    """Use cfg and the other accounts of its config file for db, people and bridge clients.
    Closes the bridge clients of an earlier connect."""
    close()
    store = db.connect(cfg)
    people.connect(cfg, store=store)
    return store


def names() -> tuple[str, ...]:
    """The configured accounts, "default" first."""
    return db.store().cfg.account_names


def handles(account: str | None = None) -> Handles:
    """Config, database and name lookups of an account (default: the current one).
    An unknown name raises config.ConfigError."""
    st = db.store(account)
    name = st.cfg.account
    return Handles(name, st.cfg, st, people.people(name))


def bridge(account: str | None = None) -> Bridge:
    """Client for the bridge of an account (default: the current one), created once per account."""
    st = db.store(account)
    name = st.cfg.account
    if st.cfg.demo:
        raise config_mod.ConfigError("The offline demo has no bridge")
    with _lock:
        found = _bridges.get(name)
        if found is None:
            found = _bridges[name] = Bridge(cfg=st.cfg)
        return found


def close() -> None:
    """Close the bridge clients."""
    with _lock:
        clients = list(_bridges.values())
        _bridges.clear()
    for c in clients:
        c.close()


@contextlib.contextmanager
def use(account: str | None) -> Iterator[Handles]:
    """Run the block on one account (None = the account of connect())."""
    with db.use_account(account):
        yield handles()


def mask_number(digits: str) -> str:
    """'+1 *** *** 0101' for 15550100101, as the bridge shows numbers; "" when too short."""
    d = "".join(ch for ch in digits if ch.isdigit())
    return f"+{d[:1]} *** *** {d[-4:]}" if len(d) >= 7 else ""


def _relative(base: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def status(account: str | None = None) -> dict[str, Any]:
    """Name, whether the number is linked, whether its bridge runs and is connected, the masked number
    and the store folder (relative to the config file when it is below it). No message data."""
    h = handles(account)
    mine = [j for j in h.people.my_jids() if people.server_of(j) == people.PHONE_SERVER]
    out: dict[str, Any] = {
        "name": h.name,
        "default": h.name == DEFAULT,
        "linked": bool(mine),
        "number": mask_number(people.user_of(mine[0])) if mine else "",
        "store": _relative(h.cfg.dir, h.cfg.store_dir),
        "database": h.cfg.wa_db.is_file(),
        "sending_enabled": h.cfg.sending.enabled,
    }
    if h.cfg.demo:
        out.update(demo=True, linked=False, connected=False, bridge_running=False)
        return out
    try:
        st = h.bridge.status()
    except BridgeError as exc:
        out.update(bridge_running=False, connected=False, bridge_error=exc.message)
        return out
    out.update(bridge_running=True, connected=bool(st.get("connected")))
    if st.get("logged_in"):
        out["linked"] = True
    if st.get("account"):
        out["number"] = str(st["account"])
    return out
