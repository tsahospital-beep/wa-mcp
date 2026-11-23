"""Several accounts in one config: inheritance, backward compatibility and per-account handles."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import synth

from wa_mcp import accounts, config, db, people
from wa_mcp.insight import my_day

WORK_ME = synth.WORK_ME
DANA = synth.DANA
build_work_store = synth.build_work_store


def write(root: Path, data: dict[str, Any]) -> Path:
    path = root / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WA_MCP_CONFIG",
        "WA_MCP_STORE_DIR",
        "WA_MCP_API_LISTEN",
        "WA_MCP_API_TOKEN",
        "WA_MCP_SENDING_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def two(tmp_path: Path) -> Path:
    path = write(
        tmp_path,
        {
            "api": {"listen": "127.0.0.1:1", "token": "test-token-0123456789"},
            "sending": {"enabled": True, "limits": {"per_chat_per_hour": 7, "per_hour": 30, "per_day": 100}},
            "tasks": {"min_score": 40},
            "accounts": {
                "default": {},
                "work": {
                    "api": {"listen": "127.0.0.1:2"},
                    "sending": {"allow_confirm_flag": True, "limits": {"per_hour": 5}},
                },
            },
        },
    )
    (tmp_path / "store").mkdir()
    synth.build_wa_db(tmp_path / "store" / "wa.db")
    synth.build_session_db(tmp_path / "store" / "session.db")
    build_work_store(tmp_path / "store" / "work")
    return path


def test_config_without_accounts_is_unchanged(tmp_path: Path) -> None:
    path = write(
        tmp_path, {"store_dir": "data", "api": {"listen": "127.0.0.1:9100"}, "sending": {"enabled": True}}
    )
    cfg = config.load(path)
    assert cfg.account == "default" and cfg.account_names == ("default",)
    assert cfg.store_dir == tmp_path / "data" and cfg.wa_db == tmp_path / "data" / "wa.db"
    assert cfg.insight_db == tmp_path / "data" / "insight.db"
    assert cfg.downloads_dir == tmp_path / "downloads" and cfg.exports_dir == tmp_path / "exports"
    assert cfg.api.listen == "127.0.0.1:9100" and cfg.sending.enabled is True
    assert cfg.raw is cfg.top_raw and cfg.raw["accounts"] == {}
    assert cfg.for_account(None) is cfg and cfg.for_account("default") is cfg
    assert config.load(path, account="default") == cfg
    with pytest.raises(config.ConfigError, match='account "work" is not configured'):
        cfg.for_account("work")
    # a missing file still means defaults
    empty = config.load(tmp_path / "nothing.json")
    assert empty.store_dir == tmp_path / "store" and empty.account_names == ("default",)


def test_account_inherits_and_overrides(two: Path) -> None:
    root = two.parent
    default = config.load(two)
    work = config.load(two, account="work")
    assert default.account_names == work.account_names == ("default", "work")
    assert default.store_dir == root / "store" and default.api.listen == "127.0.0.1:1"
    assert default.sending.limits.per_hour == 30 and default.sending.allow_confirm_flag is False
    assert work.account == "work"
    assert work.store_dir == root / "store" / "work" and work.wa_db == root / "store" / "work" / "wa.db"
    assert work.insight_db == root / "store" / "work" / "insight.db"
    assert work.downloads_dir == root / "downloads" / "work" and work.exports_dir == root / "exports" / "work"
    assert work.api.listen == "127.0.0.1:2" and work.api.token == "test-token-0123456789"
    lim = work.sending.limits
    assert (lim.per_chat_per_hour, lim.per_hour, lim.per_day) == (7, 5, 100)
    assert work.sending.enabled is True and work.sending.allow_confirm_flag is True
    assert work.section("tasks")["min_score"] == 40
    assert default.for_account("work") == work and work.for_account("default") == default
    assert work.for_account("work") is work


def test_explicit_folders_win(tmp_path: Path) -> None:
    club_store = tmp_path / "elsewhere" / "club"
    path = write(
        tmp_path,
        {
            "accounts": {
                "club": {
                    "store_dir": str(club_store),
                    "exports_dir": "out",
                    "api": {"listen": "localhost:8082"},
                }
            }
        },
    )
    club = config.load(path, account="club")
    assert club.store_dir == club_store and club.exports_dir == tmp_path / "out"
    assert club.downloads_dir == tmp_path / "downloads" / "club"


@pytest.mark.parametrize(
    ("accounts_block", "message"),
    [
        ({"work": {}}, "accounts.work.api.listen: set an address"),
        ({"work": {"api": {"listen": "localhost:8080"}}}, "port 8080 is already used by account default"),
        (
            {"work": {"api": {"listen": "127.0.0.1:8081"}, "store_dir": "store"}},
            "is already used by account default",
        ),
        ({"Work Phone": {"api": {"listen": "127.0.0.1:8081"}}}, "not a valid account name"),
        ({"work": "yes"}, "accounts.work: must be an object"),
        ({"work": {"api": "127.0.0.1:8081"}}, "accounts.work.api: must be an object"),
    ],
)
def test_account_checks(tmp_path: Path, accounts_block: dict[str, Any], message: str) -> None:
    path = write(tmp_path, {"accounts": accounts_block})
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)


def test_login_ports_must_differ(tmp_path: Path) -> None:
    path = write(
        tmp_path, {"login": {"port": 8770}, "accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}}}}
    )
    with pytest.raises(config.ConfigError, match=r"accounts\.work\.login\.port: 8770"):
        config.load(path)
    path = write(
        tmp_path,
        {
            "login": {"port": 8770},
            "accounts": {"work": {"api": {"listen": "127.0.0.1:8081"}, "login": {"port": 0}}},
        },
    )
    assert config.load(path, account="work").account == "work"


def test_env_overrides_the_top_level_only(two: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WA_MCP_API_LISTEN", "127.0.0.1:9200")
    monkeypatch.setenv("WA_MCP_STORE_DIR", "data")
    default = config.load(two)
    work = config.load(two, account="work")
    assert default.api.listen == "127.0.0.1:9200" and work.api.listen == "127.0.0.1:2"
    assert default.store_dir == two.parent / "data" and work.store_dir == two.parent / "data" / "work"


def test_db_and_people_route_to_the_account(two: Path) -> None:
    accounts.connect(config.load(two))
    assert accounts.names() == ("default", "work")
    assert db.store().path == two.parent / "store" / "wa.db"
    assert db.store("work").path == two.parent / "store" / "work" / "wa.db"
    assert db.store("work") is db.store("work")
    assert db.messages(query="quarterly") == []
    with accounts.use("work") as h:
        assert h.name == "work" and db.account() == "work"
        assert [m["msg_id"] for m in db.messages(query="quarterly")] == ["W1"]
        assert people.name_of(DANA) == "Dana Work"
        assert WORK_ME in people.my_jids()
        # a thread started here does not inherit the account: it works on the default
        seen: list[str] = []
        t = threading.Thread(target=lambda: seen.append(db.account()))
        t.start()
        t.join()
        assert seen == ["default"]
    assert db.account() == "default" and WORK_ME not in people.my_jids()
    assert people.people("work").cfg.account == "work"
    with pytest.raises(config.ConfigError, match='account "club"'), db.use_account("club"):
        pass


def test_status_without_running_bridges(two: Path) -> None:
    accounts.connect(config.load(two))
    work = accounts.status("work")
    assert work["name"] == "work" and work["linked"] is True and work["number"] == "+1 *** *** 0009"
    assert work["bridge_running"] is False and work["connected"] is False
    assert work["store"] == "store/work" and work["database"] is True
    assert accounts.status()["number"] == "+1 *** *** 0001"
    assert accounts.mask_number("1555") == "" and accounts.mask_number("+1 555 010 0101") == "+1 *** *** 0101"
    accounts.close()


def test_my_day_merges_accounts(two: Path) -> None:
    accounts.connect(config.load(two))
    now = synth.T0 + 3600
    day = my_day.my_day_accounts(now=now, lookback_days=30)
    assert day["accounts"] == ["default", "work"] and "skipped" not in day
    by_account = {g["account"] for g in day["people"]}
    assert by_account == {"default", "work"}
    assert all(it["account"] == g["account"] for g in day["people"] for it in g["items"])
    dana = [g for g in day["people"] if g["account"] == "work" and g["chat"] == DANA]
    assert dana and dana[0]["items"][0]["message_id"] == "W1"
    waiting = [it for g in day["people"] for it in g["items"] if it["type"] == "waiting_reply"]
    assert day["counts"]["waiting_for_reply"] == len(waiting) >= 2
    # insight.db is per account
    assert (two.parent / "store" / "insight.db").is_file()
    assert (two.parent / "store" / "work" / "insight.db").is_file()
    # one account only, and a chat that only one account has
    only = my_day.my_day_accounts(now=now, lookback_days=30, chat="Dana")
    assert only["accounts"] == ["work"] and only["skipped"][0]["account"] == "default"
    single = my_day.my_day_accounts(now=now, lookback_days=30, accounts=["work"])
    assert {g["account"] for g in single["people"]} == {"work"}


def test_my_day_skips_an_account_without_database(two: Path) -> None:
    (two.parent / "store" / "work" / "wa.db").unlink()
    accounts.connect(config.load(two))
    day = my_day.my_day_accounts(now=synth.T0 + 3600, lookback_days=30)
    assert day["accounts"] == ["default"] and day["skipped"][0]["account"] == "work"
    assert "no database" in day["skipped"][0]["reason"]
