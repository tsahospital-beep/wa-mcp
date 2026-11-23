"""Store keys and the API token in the OS keystore, read the same way as the bridge."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
import synth

from wa_mcp import client, config, crypt, db, keystore

VECTORS = json.loads((synth.REPO / "testdata" / "keystore_vectors.json").read_text(encoding="utf-8"))
STORE_KEY = bytes.fromhex(VECTORS["store_key_hex"])
FAKE_CLI = Path(__file__).with_name("fake_keystore_cli.py")
TOKEN = "5e" * 32
STORE_ID = "0123456789abcdef0123456789abcdef"


def test_field_key_matches_shared_vectors() -> None:
    salt = base64.b64decode(VECTORS["salt_b64"])
    assert crypt.FIELD_KEY_INFO.decode() == VECTORS["info"]
    key = crypt.field_key(STORE_KEY, salt)
    assert key.hex() == VECTORS["field_key_hex"]
    sealer = crypt.Sealer(key)
    assert sealer.verify_check(VECTORS["check"])
    assert sealer.open_string(VECTORS["sealed"]) == VECTORS["sealed_plain"]
    assert sealer.open_blob(VECTORS["blob"].encode()).hex() == VECTORS["blob_plain_hex"]
    assert crypt.field_key(STORE_KEY, b"another salt....") != key
    with pytest.raises(crypt.CryptError):
        crypt.field_key(STORE_KEY[:31], salt)
    assert keystore.dpapi_file("store-key") == VECTORS["dpapi_files"]["store-key"]
    assert keystore.dpapi_file("api-token") == VECTORS["dpapi_files"]["api-token"]
    assert keystore.dpapi_entropy("api-token").decode() == VECTORS["dpapi_entropy"]["api-token"]


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists on Windows only")
def test_dpapi_round_trip_and_other_user(tmp_path: Path) -> None:
    entropy = keystore.dpapi_entropy(keystore.ITEM_STORE_KEY)
    wrapped = keystore.protect(STORE_KEY, entropy)
    assert STORE_KEY not in wrapped
    assert keystore.unprotect(wrapped, entropy) == STORE_KEY
    # Another user's or computer's master key, simulated by a different entropy or a damaged blob.
    with pytest.raises(keystore.KeystoreError):
        keystore.unprotect(wrapped, keystore.dpapi_entropy(keystore.ITEM_API_TOKEN))
    damaged = bytearray(wrapped)
    damaged[-5] ^= 0xFF
    with pytest.raises(keystore.KeystoreError):
        keystore.unprotect(bytes(damaged), entropy)

    assert keystore.store_key(tmp_path) is None
    (tmp_path / "key.dpapi").write_bytes(wrapped)
    assert keystore.store_key(tmp_path) == STORE_KEY
    (tmp_path / "key.dpapi").write_bytes(bytes(damaged))
    with pytest.raises(keystore.KeystoreError, match="another Windows user"):
        keystore.store_key(tmp_path)


def _fake_programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put fake security and secret-tool programs first on PATH; returns the item file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in ("security", "secret-tool"):
        if sys.platform == "win32":
            (bin_dir / f"{program}.cmd").write_text(
                f'@"{sys.executable}" "{FAKE_CLI}" {program} %*\r\n', encoding="utf-8"
            )
        else:
            script = bin_dir / program
            script.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" {program} "$@"\n', encoding="utf-8"
            )
            script.chmod(0o755)
    items = tmp_path / "items.json"
    monkeypatch.setenv("FAKE_KEYSTORE_DB", str(items))
    monkeypatch.delenv("FAKE_KEYSTORE_FAIL", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return items


def test_cli_backends_with_fake_programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    items = _fake_programs(tmp_path, monkeypatch)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    for keychain in (True, False):
        assert keystore._cli_get(store_dir, keystore.ITEM_STORE_KEY, keychain) is None  # no store id yet
    (store_dir / keystore.ID_FILE).write_text(STORE_ID + "\n", encoding="ascii")
    items.write_text(
        json.dumps(
            {
                f"wa-mcp/store-key.{STORE_ID}": STORE_KEY.hex(),
                f"wa-mcp/api-token.{STORE_ID}": TOKEN.encode().hex(),
            }
        ),
        encoding="utf-8",
    )
    assert f"store-key.{STORE_ID}" == VECTORS["keychain_account"]
    for keychain in (True, False):
        assert keystore._cli_get(store_dir, keystore.ITEM_STORE_KEY, keychain) == STORE_KEY
        assert keystore._cli_get(store_dir, keystore.ITEM_API_TOKEN, keychain) == TOKEN.encode()
    items.write_text("{}", encoding="utf-8")
    for keychain in (True, False):
        assert keystore._cli_get(store_dir, keystore.ITEM_STORE_KEY, keychain) is None
    argv = Path(str(items) + ".argv").read_text(encoding="utf-8")
    assert STORE_KEY.hex() not in argv and TOKEN not in argv

    monkeypatch.setenv("FAKE_KEYSTORE_FAIL", "1")
    for keychain in (True, False):
        with pytest.raises(keystore.Unavailable, match="D-Bus"):
            keystore._cli_get(store_dir, keystore.ITEM_STORE_KEY, keychain)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(keystore.Unavailable, match="not installed"):
        keystore._cli_get(store_dir, keystore.ITEM_STORE_KEY, False)
    (store_dir / keystore.ID_FILE).write_text("not an id", encoding="ascii")
    with pytest.raises(keystore.KeystoreError, match="damaged"):
        keystore._store_id(store_dir)


def test_key_file_rules(tmp_path: Path) -> None:
    for data in (STORE_KEY.hex().encode() + b"\n", base64.b64encode(STORE_KEY), STORE_KEY):
        assert keystore.parse_key(data) == STORE_KEY
    for bad in (b"", b"short", b"zz" * 32, b"a" * 65):
        with pytest.raises(keystore.KeystoreError):
            keystore.parse_key(bad)
    path = tmp_path / "store.key"
    path.write_text(STORE_KEY.hex(), encoding="ascii")
    _private(path)
    assert keystore.read_key_file(path) == STORE_KEY
    if sys.platform != "win32":
        path.chmod(0o644)
        with pytest.raises(keystore.KeystoreError, match="chmod 600"):
            keystore.read_key_file(path)
    with pytest.raises(keystore.KeystoreError):
        keystore.read_key_file(tmp_path / "missing.key")


def _key_store(tmp_path: Path, kind: str, **encryption: object) -> Path:
    """A synthetic wa.db sealed with a field key of kind (keystore or keyfile)."""
    sealer = crypt.Sealer(crypt.field_key(STORE_KEY, synth.SALT))
    path = synth.make_config(tmp_path, encryption=encryption)
    wa = tmp_path / "store" / "wa.db"
    synth.build_wa_db(wa, sealer=sealer)
    conn = sqlite3.connect(wa)
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'crypt_check'", (sealer.seal_string(crypt.CHECK_PLAINTEXT),)
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('crypt_kind', ?)", (kind,))
    conn.commit()
    conn.close()
    return path


def test_keystore_and_keyfile_stores_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held: dict[str, bytes | None] = {"key": STORE_KEY}
    calls: list[str] = []

    def fake_get(store_dir: Path, item: str) -> bytes | None:
        calls.append(item)
        return held["key"] if item == keystore.ITEM_STORE_KEY else None

    monkeypatch.setattr(keystore, "get", fake_get)
    cfg_path = _key_store(tmp_path / "a", "keystore")
    s = db.connect(config.load(cfg_path))
    assert s.encrypted
    assert [m["msg_id"] for m in db.messages(query="große")] == ["B1"]
    db.put_derived(synth.BOB, "B1", "document", "text from a PDF")
    db.put_derived(synth.BOB, "B1", "document", "text from a PDF, again")
    assert calls.count(keystore.ITEM_STORE_KEY) == 1  # the key is read once per store
    conn = sqlite3.connect(tmp_path / "a" / "store" / "wa.db")
    raw = conn.execute("SELECT content FROM derived_text WHERE msg_id = 'B1'").fetchone()[0]
    conn.close()
    assert raw.startswith("wae2:")

    held["key"] = bytes(32)
    with pytest.raises(db.StoreError, match="keystore key does not open"):
        db.connect(config.load(cfg_path)).messages()
    held["key"] = None
    with pytest.raises(db.StoreError, match="copied from another computer"):
        db.connect(config.load(cfg_path)).messages()
    off = synth.make_config(tmp_path / "a", encryption={"mode": "off"})
    with pytest.raises(db.StoreError, match=r"encryption\.mode is off"):
        db.connect(config.load(off)).messages()

    key_file = tmp_path / "store.key"
    key_file.write_text(STORE_KEY.hex(), encoding="ascii")
    _private(key_file)
    cfg_path = _key_store(tmp_path / "b", "keyfile", mode="keyfile", key_file=str(key_file))
    db.connect(config.load(cfg_path))
    assert db.message(synth.ALICE, "A2")["body"] == "Crème BRÛLÉE at seven"


def test_token_from_keystore_then_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WA_MCP_API_TOKEN", "WA_MCP_ENCRYPTION_MODE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    (tmp_path / "store").mkdir()
    stored: dict[str, bytes] = {}
    monkeypatch.setattr(keystore, "get", lambda store_dir, item: stored.get(item))
    cfg = config.load(path)
    assert cfg.encryption.mode == "keystore" and client.load_token(cfg) == ""
    (tmp_path / "store" / "api.token").write_text("f1" * 32 + "\n", encoding="utf-8")
    assert client.load_token(cfg) == "f1" * 32  # no keystore item yet: the file
    stored[keystore.ITEM_API_TOKEN] = TOKEN.encode()
    assert client.load_token(cfg) == TOKEN

    def broken(store_dir: Path, item: str) -> bytes | None:
        raise keystore.Unavailable("secret-tool is not installed")

    monkeypatch.setattr(keystore, "get", broken)
    assert client.load_token(cfg) == "f1" * 32
    path.write_text(json.dumps({"encryption": {"mode": "off"}}), encoding="utf-8")
    monkeypatch.setattr(keystore, "get", lambda store_dir, item: TOKEN.encode())
    assert client.load_token(config.load(path)) == "f1" * 32


def test_encryption_config_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WA_MCP_ENCRYPTION_MODE", "WA_MCP_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    cases = [
        ({}, "keystore"),
        ({"encryption": {"enabled": True, "passphrase_env": "P"}}, "passphrase"),
        ({"encryption": {"enabled": False}}, "keystore"),
        ({"encryption": {"enabled": True, "mode": "off"}}, "off"),
    ]
    for body, mode in cases:
        path.write_text(json.dumps(body), encoding="utf-8")
        cfg = config.load(path)
        assert cfg.encryption.mode == mode, body
        assert cfg.encryption.enabled == (mode != "off")
    path.write_text(json.dumps({"encryption": {"mode": "keyfile", "key_file": "secret/k"}}), encoding="utf-8")
    assert config.load(path).encryption.key_file == str(tmp_path / "secret" / "k")
    for bad in ({"mode": "secret"}, {"mode": "keyfile"}, {"mode": "passphrase", "passphrase_env": ""}):
        path.write_text(json.dumps({"encryption": bad}), encoding="utf-8")
        with pytest.raises(config.ConfigError, match="encryption"):
            config.load(path)
    monkeypatch.setenv("WA_MCP_ENCRYPTION_MODE", "keyfile")
    monkeypatch.setenv("WA_MCP_KEY_FILE", str(tmp_path / "k"))
    path.write_text("{}", encoding="utf-8")
    assert config.load(path).encryption.mode == "keyfile"


def test_logs_hold_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        keystore, "get", lambda store_dir, item: STORE_KEY if item == "store-key" else TOKEN.encode()
    )
    cfg_path = _key_store(tmp_path, "keystore")
    s = db.connect(config.load(cfg_path))
    conn = sqlite3.connect(tmp_path / "store" / "wa.db")
    conn.execute("UPDATE chat_messages SET body = 'wae1:AAAAdamaged' WHERE msg_id = 'B1'")
    conn.commit()
    conn.close()
    assert db.messages(conv=synth.BOB)
    assert client.load_token(config.load(cfg_path))
    monkeypatch.setattr(keystore, "get", lambda store_dir, item: bytes(32))
    with pytest.raises(db.StoreError) as err:
        db.connect(config.load(cfg_path)).messages()
    text = caplog.text + str(err.value)
    for secret in (STORE_KEY.hex(), TOKEN, "Hello from Alice", "GROSSE STRASSE"):
        assert secret not in text
    assert s.path.is_file()


GO_PROGRAM = """package main

import (
	"context"
	"os"
	"path/filepath"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/keystore"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func main() {
	dir, kind := os.Args[1], os.Args[2]
	db, err := store.Open(filepath.Join(dir, "wa.db"))
	if err != nil {
		panic(err)
	}
	ctx := context.Background()
	db.PutMessage(ctx, &store.Message{Conv: "15550100002@s.whatsapp.net", ID: "BEFORE", SentAt: 1767000000, Body: "Plain before the migration"})
	u := store.KeyPlan{NewKind: kind, Secret: func(k string, create bool) ([]byte, error) {
		if k == store.KindKeyfile {
			return keystore.ReadKeyFile(os.Args[3])
		}
		return keystore.StoreKey(keystore.ForStore(dir), create)
	}}
	if _, err := db.SetupKey(ctx, u); err != nil {
		panic(err)
	}
	db.PutMessage(ctx, &store.Message{Conv: "15550100002@s.whatsapp.net", ID: "GO1", SentAt: 1767000001, Body: "Hello from the Go bridge"})
	db.Close()
	if kind == store.KindKeystore {
		if err := keystore.ForStore(dir).Put(keystore.ItemAPIToken, []byte(os.Args[4])); err != nil {
			panic(err)
		}
	}
}
"""


_private = synth.make_private


def _run_go(store_dir: Path, *args: str) -> None:
    go = shutil.which("go")
    if go is None:
        if os.environ.get("WA_MCP_REQUIRE_GO"):
            pytest.fail("go is not on PATH")
        pytest.skip("Go is not available")
    bridge = synth.REPO / "bridge"
    with tempfile.TemporaryDirectory(prefix="wa-mcp-keystore-") as tmp:
        program = Path(tmp) / "main.go"
        program.write_text(GO_PROGRAM, encoding="utf-8")
        overlay = Path(tmp) / "overlay.json"
        target = bridge / "cmd" / "keystoreinterop" / "main.go"
        overlay.write_text(json.dumps({"Replace": {str(target): str(program)}}), encoding="utf-8")
        env = dict(os.environ, GOFLAGS="-mod=mod", GOPROXY="off")
        proc = subprocess.run(
            [go, "run", f"-overlay={overlay}", "./cmd/keystoreinterop", str(store_dir), *args],
            cwd=bridge,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    if proc.returncode != 0:
        if "cannot find module" in proc.stderr or "GOPROXY=off" in proc.stderr:
            pytest.skip("Go modules are not in the local cache")
        pytest.fail(proc.stderr[-2000:])


@pytest.mark.parametrize("kind", ["keyfile", "keystore"])
def test_store_encrypted_by_the_go_bridge_opens_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "keystore" and sys.platform != "win32":
        pytest.skip("only DPAPI is used for real here; keychain and secret-tool are tested with fakes")
    for name in ("WA_MCP_ENCRYPTION_MODE", "WA_MCP_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    store_dir = tmp_path / "store"
    key_file = tmp_path / "store.key"
    key_file.write_text(STORE_KEY.hex(), encoding="ascii")
    _private(key_file)
    _run_go(store_dir, kind, str(key_file), TOKEN)
    enc = {"mode": "keyfile", "key_file": str(key_file)} if kind == "keyfile" else {}
    path = synth.make_config(tmp_path, encryption=enc)
    cfg = config.load(path)
    db.connect(cfg)
    if kind == "keystore":
        assert client.load_token(replace(cfg, api=replace(cfg.api, token=""))) == TOKEN
    assert db.message("15550100002@s.whatsapp.net", "GO1")["body"] == "Hello from the Go bridge"
    assert db.message("15550100002@s.whatsapp.net", "BEFORE")["body"] == "Plain before the migration"
    raw = (store_dir / "wa.db").read_bytes()
    assert b"Hello from the Go bridge" not in raw and b"Plain before the migration" not in raw


def test_secret_file_read_is_bounded_after_permission_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wa_mcp import secretfile

    path = tmp_path / "key"
    path.write_bytes(b"key")
    _private(path)
    check = secretfile.check_private

    def grow(path: Path, fd: int) -> None:
        check(path, fd)
        with path.open("ab") as fh:
            fh.write(bytes(secretfile.MAX_SECRET_FILE))

    monkeypatch.setattr(secretfile, "check_private", grow)
    with pytest.raises(secretfile.SecretFileError, match="larger"):
        secretfile.read(path)
