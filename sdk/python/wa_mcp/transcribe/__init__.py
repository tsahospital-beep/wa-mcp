"""Speech to text for voice notes and audio messages.

Audio is converted to 16 kHz mono WAV with ffmpeg and split into chunks at
quiet points. Language: transcription.language when set; otherwise detected.
A chat's usual language (from earlier confident transcripts whose language was
detected, never from ones that used the hint themselves) is preferred when
the clip is short or detection is unsure. Results go to derived_text with
source "transcript".
"""

from __future__ import annotations

import itertools
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wa_mcp import audio, db, people
from wa_mcp import config as config_mod
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.media import ensure_local
from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result, TranscriptionError

__all__ = [
    "Backend",
    "BackendUnavailable",
    "Result",
    "TranscriptionError",
    "chat_language",
    "get_backend",
    "transcribe_chat",
    "transcribe_file",
    "transcribe_message",
]

SHORT_SECONDS = 8.0
SURE_CONFIDENCE = 0.7
HINT_MIN_CONFIDENCE = 0.8
HINT_MIN_SAMPLES = 3
HINT_MIN_SHARE = 0.6
AUDIO_KINDS = ("voice", "audio")


def get_backend(cfg: config_mod.Config | None = None) -> Backend:
    """The configured backend; raises BackendUnavailable with an install hint."""
    t = (cfg or db.store().cfg).transcription
    name = t.backend.lower().replace("_", "-")
    if name == "faster-whisper":
        from wa_mcp.transcribe.faster_whisper import FasterWhisper

        backend: Backend = FasterWhisper(t.model, t.device, t.compute_type)
    elif name == "openai-whisper":
        from wa_mcp.transcribe.openai_whisper import OpenAIWhisper

        backend = OpenAIWhisper(t.model, t.device)
    elif name in ("whisper.cpp", "whisper-cpp", "whispercpp"):
        from wa_mcp.transcribe.whisper_cpp import WhisperCpp

        backend = WhisperCpp(t.whisper_cpp_binary, t.whisper_cpp_model)
    elif name == "http":
        from wa_mcp.transcribe.http import HttpBackend

        backend = HttpBackend(t.http_url, t.http_model, t.http_api_key_env)
    elif name == "command":
        from wa_mcp.transcribe.command import CommandBackend

        backend = CommandBackend(t.command_argv)
    else:
        raise BackendUnavailable(
            f"unknown transcription.backend {t.backend!r}: use faster-whisper, openai-whisper, whisper.cpp, "
            "http or command"
        )
    backend.check()
    return backend


def chat_language(convs: list[str]) -> str | None:
    """The usual language of earlier confident, detected (not hinted) transcripts in these chats."""
    langs = Counter(
        str(r["lang"])
        for r in db.derived_by_source("transcript", conv=convs)
        if r.get("lang") and not r.get("lang_hinted") and (r.get("confidence") or 0.0) >= HINT_MIN_CONFIDENCE
    )
    total = sum(langs.values())
    if total < HINT_MIN_SAMPLES:
        return None
    lang, count = langs.most_common(1)[0]
    return lang if count / total >= HINT_MIN_SHARE else None


def transcribe_file(
    backend: Backend,
    path: str | Path,
    language: str | None = None,
    hint: str | None = None,
    chunk_seconds: float = 30.0,
) -> Result:
    """Transcribe any audio file. language forces a language; hint is the chat's usual one."""
    with tempfile.TemporaryDirectory(prefix="wa-mcp-stt-") as tmp:
        wav = audio.to_wav16k(path, Path(tmp) / "full.wav")
        samples, rate = audio.read_wav(wav)
        duration = len(samples) / rate if rate else 0.0
        cuts = audio.split_points(samples, rate, chunk_seconds)
        pieces: list[Path] = []
        for i, (a, b) in enumerate(itertools.pairwise(cuts)):
            pieces.append(audio.write_wav(Path(tmp) / f"part{i:03d}.wav", samples[a:b], rate))
        first = backend.transcribe(pieces[0], language)
        chosen = language or first.language
        hinted = False
        if not language and hint and first.language != hint:
            unsure = first.confidence is not None and first.confidence < SURE_CONFIDENCE
            if duration < SHORT_SECONDS or unsure:
                chosen, hinted = hint, True
                first = backend.transcribe(pieces[0], hint)
        texts = [first.text]
        for piece in pieces[1:]:
            texts.append(backend.transcribe(piece, chosen).text)
    text = " ".join(t.strip() for t in texts if t.strip())
    # The confidence belongs to the run whose text was kept.
    return Result(text, chosen, first.confidence, hinted)


def transcribe_message(
    bridge: Bridge,
    conv: str,
    msg_id: str,
    force: bool = False,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Transcribe one voice/audio message and store the transcript."""
    row = db.message(conv, msg_id)
    if row is None:
        raise LookupError(f"message {msg_id} not found in {conv}")
    if not force:
        for d in db.derived(conv, msg_id):
            if d["source"] == "transcript":
                return {"msg_id": msg_id, "text": d["content"], "lang": d["lang"], "cached": True}
    engine = backend or get_backend()
    cfg = db.store().cfg
    path = ensure_local(bridge, row)
    fixed = cfg.transcription.language
    hint = None if fixed else chat_language(people.same_person(conv))
    result = transcribe_file(
        engine, path, language=fixed, hint=hint, chunk_seconds=cfg.transcription.chunk_seconds
    )
    db.put_derived(
        conv,
        msg_id,
        "transcript",
        result.text,
        lang=result.language,
        confidence=result.confidence,
        hinted=result.hinted,
    )
    return {"msg_id": msg_id, "text": result.text, "lang": result.language, "cached": False}


def transcribe_chat(
    bridge: Bridge,
    chat: str,
    after: db.TimeArg = None,
    limit: int = 50,
    force: bool = False,
    backend: Backend | None = None,
    budget_seconds: float = 120.0,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Transcribe voice/audio messages of a chat sent after `after` (all when None).

    Stops after `limit` new transcripts or when the time budget is used up; already
    transcribed messages are skipped, so calling again continues.
    """
    engine = backend or get_backend()
    deadline = clock() + budget_seconds
    rows = db.messages(conv=people.same_person(chat), after=after, kind=list(AUDIO_KINDS), limit=None)
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    cached = 0
    handled = 0
    stopped = ""
    for row in rows:
        if len(done) >= limit:
            stopped = "limit reached"
            break
        if clock() >= deadline:
            stopped = "time budget used up"
            break
        handled += 1
        try:
            res = transcribe_message(
                bridge, str(row["conv"]), str(row["msg_id"]), force=force, backend=engine
            )
        except (BridgeError, TranscriptionError, audio.AudioError, ValueError) as exc:
            failed.append({"msg_id": row["msg_id"], "error": str(exc)})
            continue
        if res["cached"]:
            cached += 1
            continue
        done.append(res)
    out: dict[str, Any] = {
        "transcribed": done,
        "already_done": cached,
        "failed": failed,
        "candidates": len(rows),
        "remaining": len(rows) - handled,
        "complete": handled == len(rows),
    }
    if handled < len(rows):
        out["stopped"] = stopped
        out["resume"] = "call again with the same arguments and force=false to continue"
    return out
