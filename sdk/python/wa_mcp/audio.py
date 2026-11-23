"""Audio and image conversion with ffmpeg (no shell, fixed argument lists)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path

from wa_mcp import db, fsutil

FFMPEG_ENV = "WA_MCP_FFMPEG"
_TIMEOUT = 600


class AudioError(Exception):
    """ffmpeg is missing or failed."""


def ffmpeg_path() -> str:
    found = os.environ.get(FFMPEG_ENV) or shutil.which("ffmpeg")
    if not found:
        raise AudioError(
            f"ffmpeg not found: install it (https://ffmpeg.org) and put it on PATH, or set {FFMPEG_ENV}"
        )
    return found


def _run(args: list[str]) -> None:
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg timed out") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise AudioError("ffmpeg failed: " + " | ".join(tail))


def outgoing_path(name: str) -> Path:
    """A path in a new private folder under exports_dir/outgoing; never next to the source file."""
    root = fsutil.private_dir(db.store().cfg.exports_dir / "outgoing")
    return Path(tempfile.mkdtemp(prefix="send-", dir=root)) / name


def to_voice_note(src: str | Path, dst: str | Path | None = None) -> Path:
    """Convert any audio file to a WhatsApp voice note (Ogg/Opus, mono, 48 kHz).

    Without dst the result goes to a new folder under exports_dir/outgoing (see outgoing_path).
    """
    source = Path(src)
    if not source.is_file():
        raise AudioError(f"no such file: {source}")
    target = Path(dst) if dst else outgoing_path(source.stem + ".ogg")
    _run(
        [
            "-i", str(source), "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", str(target),
        ]
    )  # fmt: skip
    return target


def to_wav16k(src: str | Path, dst: str | Path) -> Path:
    """16 kHz mono 16-bit PCM WAV, the input format of every transcription backend."""
    target = Path(dst)
    _run(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(target)])
    return target


def to_sticker(src: str | Path, dst: str | Path | None = None) -> Path:
    """Convert an image to a 512x512 WebP sticker with a transparent border (default place as in
    to_voice_note)."""
    source = Path(src)
    if not source.is_file():
        raise AudioError(f"no such file: {source}")
    target = Path(dst) if dst else outgoing_path(source.stem + ".webp")
    scale = (
        "scale=512:512:force_original_aspect_ratio=decrease,format=rgba,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
    )
    _run(
        ["-i", str(source), "-vf", scale, "-frames:v", "1", "-c:v", "libwebp", "-quality", "80", str(target)]
    )
    return target


def read_wav(path: str | Path) -> tuple[array[int], int]:
    """Samples (int16) and sample rate of a mono 16-bit WAV."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise AudioError("expected mono 16-bit WAV")
        rate = w.getframerate()
        data = array("h")
        data.frombytes(w.readframes(w.getnframes()))
    return data, rate


def write_wav(path: str | Path, samples: array[int], rate: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return Path(path)


def split_points(
    samples: array[int], rate: int, chunk_seconds: float, search_seconds: float = 3.0
) -> list[int]:
    """Cut positions near every chunk_seconds, moved to the quietest 20 ms frame nearby."""
    total = len(samples)
    step = int(chunk_seconds * rate)
    if step <= 0 or total <= step + rate:
        return [0, total]
    frame = max(rate // 50, 1)
    cuts = [0]
    pos = step
    while pos < total - rate:
        lo = max(cuts[-1] + frame, pos - int(search_seconds * rate))
        hi = min(total - frame, pos + int(search_seconds * rate))
        best, best_energy = pos, None
        for start in range(lo, hi, frame):
            seg = samples[start : start + frame]
            energy = sum(x * x for x in seg)
            if best_energy is None or energy < best_energy:
                best, best_energy = start, energy
        cuts.append(best)
        pos = best + step
    cuts.append(total)
    return cuts
