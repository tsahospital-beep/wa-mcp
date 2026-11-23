from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import wave
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import synth

from wa_mcp import audio, config, db, transcribe
from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result
from wa_mcp.transcribe.command import CommandBackend
from wa_mcp.transcribe.http import HttpBackend

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def tone(path: Path, seconds: float, rate: int = 16000) -> Path:
    """Tone with a silent gap every 5 seconds."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            t = i / rate
            value = 0 if (t % 5.0) > 4.5 else int(8000 * math.sin(2 * math.pi * 440 * t))
            frames += value.to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))
    return path


def test_split_points_prefer_silence() -> None:
    from array import array

    rate = 1000
    samples = array("h", [1000] * (12 * rate))
    for i in range(int(4.6 * rate), int(4.9 * rate)):
        samples[i] = 0
    cuts = audio.split_points(samples, rate, chunk_seconds=5, search_seconds=1)
    assert cuts[0] == 0 and cuts[-1] == len(samples)
    assert int(4.6 * rate) <= cuts[1] < int(4.9 * rate)


def test_command_backend(tmp_path: Path) -> None:
    script = tmp_path / "stt.py"
    script.write_text(
        "import sys; print('heard', sys.argv[1].endswith('.wav'), sys.argv[2])", encoding="utf-8"
    )
    backend = CommandBackend([sys.executable, str(script), "{file}", "{language}"])
    backend.check()
    res = backend.transcribe(tmp_path / "x.wav", "el")
    assert res.text == "heard True el" and res.language == "el"
    with pytest.raises(BackendUnavailable, match="argv"):
        CommandBackend([]).check()
    with pytest.raises(BackendUnavailable, match="fixed program"):
        CommandBackend(["{language}", "{file}"]).check()
    if os.name == "nt":
        with pytest.raises(BackendUnavailable, match="native executable"):
            CommandBackend(["stt.cmd", "{file}"]).check()
    bad = CommandBackend(
        [sys.executable, "-c", "import sys; sys.stderr.write('private words'); sys.exit(3)", "{file}"]
    )
    with pytest.raises(transcribe.TranscriptionError, match="exited with 3") as caught:
        bad.transcribe(tmp_path / "x.wav")
    assert "private words" not in str(caught.value)


def test_ffmpeg_errors_hide_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["stderr"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 2)

    monkeypatch.setenv(audio.FFMPEG_ENV, sys.executable)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(audio.AudioError, match="ffmpeg exited with 2") as caught:
        audio._run(["private-name.wav"])
    assert "private-name" not in str(caught.value)
    if os.name == "nt":
        monkeypatch.setenv(audio.FFMPEG_ENV, str(tmp_path / "tool.cmd"))
        with pytest.raises(audio.AudioError, match="native executable"):
            audio.ffmpeg_path()


class _Fake(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Fake.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"text": " hello world ", "language": "greek"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def fake_stt() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1/audio/transcriptions"
    server.shutdown()


def test_http_backend(tmp_path: Path, fake_stt: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_STT_KEY", "sk-test-123")
    wav = tone(tmp_path / "a.wav", 0.5)
    res = HttpBackend(fake_stt, "whisper-1", "TEST_STT_KEY").transcribe(wav, None)
    assert res.text == "hello world" and res.language == "el"
    call = _Fake.calls[-1]
    assert call["auth"] == "Bearer sk-test-123"
    assert b'name="model"' in call["body"] and b"verbose_json" in call["body"]


def test_missing_backends_explain_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "whisper", None)
    for name, hint in (
        ("faster-whisper", "uv sync --all-packages --inexact --extra faster-whisper"),
        ("openai-whisper", 'uv pip install -e "./sdk/python[openai-whisper]"'),
        ("whisper.cpp", "transcription.whisper_cpp.binary"),
        ("nonsense", "unknown transcription.backend"),
    ):
        path = synth.make_config(tmp_path / name, transcription={"backend": name})
        with pytest.raises(BackendUnavailable) as err:
            transcribe.get_backend(config.load(path))
        assert hint in str(err.value)


class Scripted(Backend):
    """Detects 'xx' with low confidence unless a language is forced."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def check(self) -> None:
        return

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        self.calls.append(language)
        return Result(f"part-{language or 'auto'}", language or "xx", 0.4 if language is None else 0.99)


@needs_ffmpeg
def test_chunking_and_chat_language_hint(tmp_path: Path, store: db.Store) -> None:
    long_clip = tone(tmp_path / "long.wav", 12.0)
    backend = Scripted()
    res = transcribe.transcribe_file(backend, long_clip, chunk_seconds=5)
    assert backend.calls == [None, "xx", "xx"]
    assert res.text == "part-auto part-xx part-xx"

    for i in range(3):
        db.put_derived(synth.ALICE, f"H{i}", "transcript", "earlier", lang="el", confidence=0.95)
    assert transcribe.chat_language([synth.ALICE, synth.ALICE_LID]) == "el"
    short = tone(tmp_path / "short.wav", 2.0)
    backend = Scripted()
    res = transcribe.transcribe_file(backend, short, hint="el")
    assert backend.calls == [None, "el"] and res.language == "el" and res.text == "part-el"


@needs_ffmpeg
def test_transcribe_message_stores_transcript(tmp_path: Path, store: db.Store) -> None:
    store.cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
    clip = tone(store.cfg.downloads_dir / "voice.wav", 1.0)

    class FakeBridge:
        def download_media(self, chat: str, msg_id: str) -> dict[str, Any]:
            return {"path": str(clip)}

    backend = Scripted()
    out = transcribe.transcribe_chat(FakeBridge(), synth.ALICE, backend=backend, force=True)  # type: ignore[arg-type]
    assert out["transcribed"][0]["msg_id"] == "A3"
    row = db.derived(synth.ALICE, "A3")[0]
    assert row["source"] == "transcript" and row["content"] == "part-auto"


@needs_ffmpeg
def test_voice_note_conversion(tmp_path: Path) -> None:
    ogg = audio.to_voice_note(tone(tmp_path / "say.wav", 1.0), tmp_path / "say.ogg")
    assert ogg.read_bytes()[:4] == b"OggS"


def _speech_wav(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    script = (
        "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{path}'); $s.Speak('Hello, the book club meets on Friday.'); $s.Dispose()"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60, check=False
    )
    return proc.returncode == 0 and path.is_file()


@needs_ffmpeg
def test_faster_whisper_smoke(tmp_path: Path) -> None:
    pytest.importorskip("faster_whisper", reason="faster-whisper not installed in this environment")
    if os.environ.get("WA_MCP_SKIP_MODEL_DOWNLOAD"):
        pytest.skip("model download disabled")
    wav = tmp_path / "speech.wav"
    if not _speech_wav(wav):
        pytest.skip("no speech synthesizer available")
    from wa_mcp.transcribe.faster_whisper import FasterWhisper

    res = transcribe.transcribe_file(FasterWhisper("tiny", "cpu", "int8"), wav)
    assert res.language == "en"
    assert "book" in res.text.casefold()


def test_windows_pyav_troubleshooting() -> None:
    guide = (Path(__file__).resolve().parents[3] / "docs/guides/transcription.md").read_text(encoding="utf-8")
    row = next(line for line in guide.splitlines() if "metadata_errors" in line)
    assert all(term in row for term in ("Windows", "WAV", "Pin", "`av`", "`command`"))
