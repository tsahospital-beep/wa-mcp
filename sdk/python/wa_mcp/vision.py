"""Text from photos and screenshots.

Two local OCR engines, chosen with vision.ocr: "tesseract" (the tesseract program, run without a shell)
and "rapidocr" (optional extra "ocr", imported only when used). "none" turns reading off. Results are
stored in derived_text with source "image", so list_messages and search find them. With describe=true an
optional description from the AI provider's vision model is stored with source "image_description".

Images with no readable text are stored as an empty "image" row, so they are not read again. Each call
stops at a count limit or at a time budget, and calling again continues, as with transcription.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wa_mcp import ai, db, install_hint, people
from wa_mcp import config as config_mod
from wa_mcp.ai import prompts
from wa_mcp.ai.base import image_media_type
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.media import ensure_local

__all__ = [
    "Engine",
    "OcrError",
    "OcrResult",
    "OcrUnavailable",
    "get_engine",
    "read_chat",
    "read_message",
]

OCR_HINT = install_hint("ocr")
TESSERACT_HINT = (
    "install the tesseract program (for example winget install UB-Mannheim.TesseractOCR, brew install "
    "tesseract or apt install tesseract-ocr) and put it on PATH, or set vision.tesseract_binary"
)
MAX_TEXT_CHARS = 20_000
# Images with more pixels are refused before any decoder sees them (decompression bombs).
MAX_PIXELS = 50_000_000
# JPEG markers that start a frame and carry its size (SOF0 to SOF15 without DHT, JPG and DAC).
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
PREVIEW_CHARS = 2000
DESCRIPTION_SOURCE = "image_description"
TEXT_SOURCE = "image"
_ALNUM = re.compile(r"\w", re.UNICODE)
_BLANK_RUN = re.compile(r"\n{3,}")


class OcrUnavailable(Exception):
    """The OCR engine is not installed or not configured; the message says how to fix it."""


class OcrError(Exception):
    """The engine ran but failed on this file, or the file is not usable."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    lang: str | None = None


class Engine(ABC):
    """Reads the text of one image file."""

    name = "base"

    @abstractmethod
    def check(self) -> None:
        """Raise OcrUnavailable when the engine cannot run."""

    @abstractmethod
    def read(self, path: Path) -> OcrResult: ...


class Tesseract(Engine):
    name = "tesseract"

    def __init__(self, binary: str, languages: tuple[str, ...], timeout: float) -> None:
        self.binary = binary
        self.languages = "+".join(languages) or "eng"
        self.timeout = timeout

    def _exe(self) -> str:
        found = shutil.which(self.binary or "tesseract")
        if found is None:
            raise OcrUnavailable(f"tesseract was not found: {TESSERACT_HINT}")
        return found

    def check(self) -> None:
        self._exe()

    def read(self, path: Path) -> OcrResult:
        argv = [self._exe(), str(path.resolve()), "stdout", "-l", self.languages]
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrError(f"tesseract took longer than {self.timeout:g} seconds") from exc
        except OSError as exc:
            raise OcrError(f"tesseract did not run: {exc.__class__.__name__}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
            raise OcrError(f"tesseract exited with {proc.returncode}: " + " | ".join(t.strip() for t in tail))
        return OcrResult(proc.stdout.decode("utf-8", "replace"), self.languages)


class RapidOcr(Engine):
    """rapidocr-onnxruntime. Its default models read Chinese and English; vision.languages is not used."""

    name = "rapidocr"

    def __init__(self) -> None:
        self._engine: Any = None

    def check(self) -> None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError as exc:
            raise OcrUnavailable(f"rapidocr is not installed: {OCR_HINT}") from exc

    def read(self, path: Path) -> OcrResult:
        self.check()
        try:
            if self._engine is None:
                from rapidocr_onnxruntime import RapidOCR

                self._engine = RapidOCR()
            result, _ = self._engine(str(path))
        except Exception as exc:
            raise OcrError(f"rapidocr failed: {exc.__class__.__name__}") from exc
        lines = [str(item[1]) for item in result or [] if len(item) > 1]
        return OcrResult("\n".join(lines), None)


def _jpeg_size(fh: Any) -> tuple[int, int] | None:
    fh.seek(2)
    for _ in range(512):
        head = fh.read(4)
        if len(head) < 4 or head[0] != 0xFF:
            return None
        marker, length = head[1], int.from_bytes(head[2:4], "big")
        if marker in _SOF:
            frame = fh.read(5)
            return (
                (int.from_bytes(frame[3:5], "big"), int.from_bytes(frame[1:3], "big"))
                if len(frame) == 5
                else None
            )
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            fh.seek(-2, 1)
            continue
        if length < 2:
            return None
        fh.seek(length - 2, 1)
    return None


def image_size(path: Path) -> tuple[str, int, int]:
    """Format, width and height read from the file header only (no decoder). Raises OcrError for other
    file types."""
    with path.open("rb") as fh:
        head = fh.read(32)
        kind = image_media_type(head[:16]) or ("image/bmp" if head[:2] == b"BM" else None)
        size: tuple[int, int] | None = None
        if kind == "image/png" and head[12:16] == b"IHDR":
            size = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        elif kind == "image/gif":
            size = int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
        elif kind == "image/bmp":
            size = (
                int.from_bytes(head[18:22], "little", signed=True),
                abs(int.from_bytes(head[22:26], "little", signed=True)),
            )
        elif kind == "image/webp":
            chunk = head[12:16]
            if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
                fh.seek(26)
                b = fh.read(4)
                size = int.from_bytes(b[0:2], "little") & 0x3FFF, int.from_bytes(b[2:4], "little") & 0x3FFF
            elif chunk == b"VP8L" and head[20] == 0x2F:
                fh.seek(21)
                bits = int.from_bytes(fh.read(4), "little")
                size = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            elif chunk == b"VP8X":
                fh.seek(24)
                b = fh.read(6)
                size = int.from_bytes(b[0:3], "little") + 1, int.from_bytes(b[3:6], "little") + 1
        elif kind == "image/jpeg":
            size = _jpeg_size(fh)
    if kind is None:
        raise OcrError("the file is not a JPEG, PNG, GIF, WebP or BMP image")
    if size is None or size[0] <= 0 or size[1] <= 0:
        raise OcrError(f"the {kind} header has no readable image size")
    return kind, size[0], size[1]


def check_image(path: Path, max_bytes: float) -> None:
    """Refuse files that are too large, not an image, or that would decode to more than MAX_PIXELS."""
    size = path.stat().st_size
    if size > max_bytes:
        raise OcrError(
            f"image is {size / 1_048_576:.1f} MB, above vision.max_image_mb ({max_bytes / 1_048_576:g})"
        )
    _, width, height = image_size(path)
    if width * height > MAX_PIXELS:
        raise OcrError(
            f"image is {width} x {height} pixels, above the limit of {MAX_PIXELS // 1_000_000} megapixels"
        )


def get_engine(cfg: config_mod.Config | None = None) -> Engine | None:
    """The configured OCR engine, or None when vision.ocr is "none". Raises OcrUnavailable with a hint."""
    v = (cfg or db.store().cfg).vision
    engine: Engine
    if v.ocr == "tesseract":
        engine = Tesseract(v.tesseract_binary, v.languages, v.timeout_s)
    elif v.ocr == "rapidocr":
        engine = RapidOcr()
    else:
        return None
    engine.check()
    return engine


def clean_text(text: str, min_chars: int) -> str:
    """Tidy OCR output; text with fewer than min_chars letters or digits counts as no text."""
    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").split("\n")
    ]
    out = _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()
    if len(_ALNUM.findall(out)) < min_chars:
        return ""
    return out[:MAX_TEXT_CHARS]


def _preview(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + " [cut]"


def read_message(
    bridge: Bridge,
    conv: str,
    msg_id: str,
    engine: Engine | None,
    provider: ai.Provider | None = None,
    force: bool = False,
    cfg: config_mod.Config | None = None,
) -> dict[str, Any]:
    """Read the text of one image (and describe it when a provider is given) and store the results."""
    c = cfg or db.store().cfg
    row = db.message(conv, msg_id)
    if row is None:
        raise LookupError(f"message {msg_id} not found in {conv}")
    if row["kind"] != "image":
        raise ValueError(f"message {msg_id} is a {row['kind']}, not an image")
    stored = {str(d["source"]): d for d in db.derived(conv, msg_id)}
    need_text = engine is not None and (force or TEXT_SOURCE not in stored)
    need_desc = provider is not None and (force or DESCRIPTION_SOURCE not in stored)
    text = str(stored[TEXT_SOURCE]["content"]) if TEXT_SOURCE in stored else None
    desc = str(stored[DESCRIPTION_SOURCE]["content"]) if DESCRIPTION_SOURCE in stored else None
    if need_text or need_desc:
        path = ensure_local(bridge, row)
        check_image(path, c.vision.max_image_mb * 1024 * 1024)
        if need_text and engine is not None:
            res = engine.read(path)
            text = clean_text(res.text, c.vision.min_text_chars)
            db.put_derived(conv, msg_id, TEXT_SOURCE, text, lang=res.lang)
        if need_desc and provider is not None:
            desc = provider.describe_image(path, prompts.IMAGE_DESCRIBE)
            db.put_derived(conv, msg_id, DESCRIPTION_SOURCE, desc)
    out: dict[str, Any] = {"msg_id": msg_id, "cached": not (need_text or need_desc)}
    if text is not None:
        out["text"] = _preview(text)
        if not text:
            out["no_text"] = True
    if desc:
        out["description"] = _preview(desc)
    return out


def read_chat(
    bridge: Bridge,
    chat: str,
    engine: Engine | None,
    provider: ai.Provider | None = None,
    after: db.TimeArg = None,
    limit: int = 50,
    force: bool = False,
    budget_seconds: float = 120.0,
    clock: Callable[[], float] = time.monotonic,
    cfg: config_mod.Config | None = None,
) -> dict[str, Any]:
    """Read the images of a chat sent after `after` (all when None), newest first.

    Stops after `limit` images worked on or when the time budget is used up; images that are done are
    skipped, so calling again continues.
    """
    if engine is None and provider is None:
        raise ValueError("nothing to do: turn on vision.ocr or ask for describe with ai.provider set")
    deadline = clock() + budget_seconds
    rows = db.messages(conv=people.same_person(chat), after=after, kind="image", limit=None)
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
            res = read_message(
                bridge, str(row["conv"]), str(row["msg_id"]), engine, provider, force=force, cfg=cfg
            )
        except ai.AILimitError as exc:
            handled -= 1
            stopped = str(exc)
            break
        except BridgeError as exc:
            failed.append({"msg_id": row["msg_id"], "error": exc.message})
            if exc.status in (0, 401, 503):
                stopped = "the bridge is not reachable"
                break
            continue
        except (OcrError, ai.AIError, ValueError, OSError) as exc:
            failed.append({"msg_id": row["msg_id"], "error": str(exc)})
            continue
        if res["cached"]:
            cached += 1
            continue
        res["time"] = row["time"]
        done.append(res)
    out: dict[str, Any] = {
        "read": done,
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
