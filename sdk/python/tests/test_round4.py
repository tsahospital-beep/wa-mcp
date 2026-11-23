from __future__ import annotations

import base64
import gzip
import json
import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import synth

from wa_mcp import audio, config, db, files, links
from wa_mcp.transcribe import http as stt
from wa_mcp.transcribe.base import BackendUnavailable, TranscriptionError


def test_transcription_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "key"
    key.write_text("sample-token\n", encoding="utf-8")
    synth.make_private(key)
    monkeypatch.delenv("TEST_STT_KEY", raising=False)
    monkeypatch.setenv("TEST_STT_KEY_FILE", str(key))
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"audio")
    with pytest.raises(BackendUnavailable, match="https"):
        stt.HttpBackend("http://speech.example.test", "speech").transcribe(wav)
    for url in ("http://[::1]/", "http://localhost/", "http://127.0.0.1/", "https://speech.example.test"):
        stt.HttpBackend(url, "speech").check()
    for url in ("https://", "https://speech.example.test:invalid"):
        with pytest.raises(BackendUnavailable):
            stt.HttpBackend(url, "speech").check()

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield gzip.compress(b'{"text":"hello"}')

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sample-token"
        return httpx.Response(200, stream=Body(), headers={"Content-Encoding": "gzip"})

    backend = stt.HttpBackend(
        "https://speech.example.test", "speech", "TEST_STT_KEY", transport=httpx.MockTransport(answer)
    )
    assert backend.transcribe(wav).text == "hello"
    monkeypatch.setattr(stt, "MAX_RESPONSE_BYTES", 8)
    with pytest.raises(TranscriptionError, match="larger than 8 bytes"):
        backend.transcribe(wav)
    key.unlink()
    with pytest.raises(BackendUnavailable, match="TEST_STT_KEY_FILE"):
        backend.transcribe(wav)


def test_simultaneous_readers_build_one_sealer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {"store_dir": ".", "encryption": {"mode": "passphrase", "passphrase_env": "TEST_WA_PASS"}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_WA_PASS", synth.PASSPHRASE)
    reader = db.Store(config.load(config_path))
    conn = sqlite3.connect(reader.path)
    with conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [("crypt_salt", base64.b64encode(synth.SALT).decode()), ("crypt_check", synth.GO_CHECK)],
        )
    conn.close()
    make_sealer = db.Sealer
    calls = []

    def build(key: bytes) -> db.Sealer:
        assert reader._setup_lock.locked()
        calls.append(1)
        return make_sealer(key)

    monkeypatch.setattr(db, "Sealer", build)
    barrier = threading.Barrier(4)

    def read() -> bool:
        barrier.wait(timeout=10)
        return reader.encrypted

    with ThreadPoolExecutor(4) as pool:
        results = [pool.submit(read) for _ in range(4)]
        assert all(f.result() for f in results)
    assert len(calls) == 1


@pytest.mark.parametrize("port", ["abc", "99999"])
def test_link_invalid_port(port: str) -> None:
    with pytest.raises(links.LinkError, match="invalid port"):
        links.check_url(f"https://example.test:{port}/")


@pytest.mark.parametrize("suffix", [".docx", ".zip"])
def test_zip_member_limit_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    path = tmp_path / ("archive" + suffix)
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("first", "second"):
            archive.writestr(name, "")

    def unexpected(*args: object) -> None:
        pytest.fail("loaded central directory before checking the member limit")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected)
    with pytest.raises(files.FileError, match="entries"):
        files.extract_text(path, files.Limits(max_members=1))


@pytest.mark.parametrize("zip64", [False, True])
def test_zip_inventory_with_leading_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zip64: bool
) -> None:
    path = tmp_path / "notes.zip"
    path.write_bytes(b"stub")
    if zip64:
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("note.txt", "short note")
        archive.comment = b"archive comment"
    assert "short note" in files.extract_text(path).text


def test_ffmpeg_uses_local_protocol_and_selected_demuxer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(audio, "_run", calls.append)
    path = tmp_path / "clip"
    path.write_bytes(b"OggS")
    audio.to_wav16k(path, tmp_path / "out.wav")
    assert calls[0][:6] == ["-protocol_whitelist", "file", "-f", "ogg", "-i", str(path)]
    path.write_bytes(b"#EXTM3U")
    with pytest.raises(audio.AudioError, match="not a supported"):
        audio.to_wav16k(path, tmp_path / "out.wav")
    assert len(calls) == 1
