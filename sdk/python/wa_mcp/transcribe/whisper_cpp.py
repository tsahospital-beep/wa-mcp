"""whisper.cpp backend: runs the whisper-cli binary on a 16 kHz mono WAV."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result, TranscriptionError

HINT = (
    "set transcription.whisper_cpp.binary (whisper-cli) and transcription.whisper_cpp.model (ggml .bin) "
    "in config.json; scripts/install_whisper.ps1 or .sh with --backend whisper.cpp shows how"
)


class WhisperCpp(Backend):
    name = "whisper.cpp"

    def __init__(self, binary: str, model: str, timeout: float = 1800.0) -> None:
        self.binary = binary
        self.model = model
        self.timeout = timeout

    def check(self) -> None:
        if not self.binary or not Path(self.binary).is_file():
            raise BackendUnavailable(f"whisper.cpp binary not found: {HINT}")
        if not self.model or not Path(self.model).is_file():
            raise BackendUnavailable(f"whisper.cpp model not found: {HINT}")

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        self.check()
        with tempfile.TemporaryDirectory(prefix="wa-mcp-wcpp-") as tmp:
            base = Path(tmp) / "out"
            argv = [
                self.binary, "-m", self.model, "-f", str(wav), "-l", language or "auto",
                "-nt", "-np", "-oj", "-of", str(base),
            ]  # fmt: skip
            try:
                proc = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TranscriptionError(f"whisper.cpp did not run: {exc}") from exc
            if proc.returncode != 0:
                tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
                raise TranscriptionError("whisper.cpp failed: " + " | ".join(tail))
            out = base.with_suffix(".json")
            if not out.is_file():
                return Result(proc.stdout.decode("utf-8", "replace").strip(), language, None)
            data = json.loads(out.read_text(encoding="utf-8", errors="replace"))
        text = " ".join(str(s.get("text", "")).strip() for s in data.get("transcription", []))
        lang = (data.get("result") or {}).get("language") or language
        return Result(text.strip(), lang, None)
