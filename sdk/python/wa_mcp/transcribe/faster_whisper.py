"""faster-whisper backend (default). Any CTranslate2 Whisper model id from Hugging Face works."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from wa_mcp import install_hint
from wa_mcp.audio import read_wav
from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result, TranscriptionError

INSTALL = install_hint("faster-whisper")

_models: dict[tuple[str, str, str], Any] = {}
_lock = threading.Lock()


class FasterWhisper(Backend):
    name = "faster-whisper"

    def __init__(self, model: str, device: str = "auto", compute_type: str = "auto") -> None:
        self.model_id = model or "large-v3"
        self.device = device or "auto"
        self.compute_type = compute_type or "auto"

    def check(self) -> None:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailable(f"faster-whisper is not installed: {INSTALL}") from exc

    def _model(self) -> Any:
        self.check()
        from faster_whisper import WhisperModel

        key = (self.model_id, self.device, self.compute_type)
        with _lock:
            if key not in _models:
                try:
                    _models[key] = WhisperModel(
                        self.model_id, device=self.device, compute_type=self.compute_type
                    )
                except Exception as exc:
                    raise BackendUnavailable(f"cannot load model {self.model_id!r}: {exc}") from exc
            return _models[key]

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        import numpy as np

        model = self._model()
        samples, _ = read_wav(wav)
        # Decoded here so faster-whisper does not depend on its PyAV file reader.
        data = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
        try:
            segments, info = model.transcribe(
                data, language=language, beam_size=5, vad_filter=False, condition_on_previous_text=False
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            raise TranscriptionError(f"faster-whisper failed: {exc}") from exc
        return Result(text, info.language, float(info.language_probability))
