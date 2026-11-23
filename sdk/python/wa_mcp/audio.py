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
    if os.name == "nt" and Path(shutil.which(found) or found).suffix.lower() in (".bat", ".cmd"):
        raise AudioError("ffmpeg must be a native executable on Windows")
    return found


def _run(args: list[str]) -> None:
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AudioError("ffmpeg timed out") from None
    except OSError as exc:
        raise AudioError(f"ffmpeg did not run: {type(exc).__name__}") from None
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg exited with {proc.returncode}")


AUDIO = "audio"
IMAGE = "image"


def input_format(path: Path, kind: str) -> str:
    """The ffmpeg demuxer for a file, chosen from its first bytes. Only plain media files are accepted:
    no playlists, concat lists or other formats that make ffmpeg open more files or URLs."""
    with path.open("rb") as fh:
        head = fh.read(16)
    sync = len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0  # MPEG audio frame
    audio = (
        (head[:4] == b"OggS", "ogg"),
        (head[:4] == b"RIFF" and head[8:12] == b"WAVE", "wav"),
        (head[:3] == b"ID3" or (sync and head[1] & 0x06 != 0), "mp3"),
        (sync and head[1] & 0xF6 == 0xF0, "aac"),  # ADTS
        (head[4:8] == b"ftyp", "mov"),
        (head[:4] == b"fLaC", "flac"),
        (head[:4] == b"\x1a\x45\xdf\xa3", "matroska"),
        (head[:5] == b"#!AMR", "amr"),
    )
    image = (
        (head[:8] == b"\x89PNG\r\n\x1a\n", "png_pipe"),
        (head[:3] == b"\xff\xd8\xff", "jpeg_pipe"),
        (head[:4] == b"RIFF" and head[8:12] == b"WEBP", "webp_pipe"),
        (head[:6] in (b"GIF87a", b"GIF89a"), "gif"),
    )
    for match, fmt in audio if kind == AUDIO else image:
        if match:
            return fmt
    wanted = (
        "audio file (Ogg, WAV, MP3, AAC, MP4/M4A, FLAC, WebM, AMR)"
        if kind == AUDIO
        else "PNG, JPEG, WebP or GIF image"
    )
    raise AudioError(f"{path.name} is not a supported {wanted}")


def _input(path: Path, kind: str) -> list[str]:
    """Input options: local files only (-protocol_whitelist file) and the demuxer set from the content."""
    return ["-protocol_whitelist", "file", "-f", input_format(path, kind), "-i", str(path)]


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
            *_input(source, AUDIO), "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", str(target),
        ]
    )  # fmt: skip
    return target


def to_wav16k(src: str | Path, dst: str | Path) -> Path:
    """16 kHz mono 16-bit PCM WAV, the input format of every transcription backend."""
    target = Path(dst)
    args = [*_input(Path(src), AUDIO), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav"]
    _run([*args, str(target)])
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
        [
            *_input(source, IMAGE),
            "-vf",
            scale,
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-quality",
            "80",
            str(target),
        ]
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
