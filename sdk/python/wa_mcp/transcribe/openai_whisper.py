"""openai-whisper backend (PyTorch)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from wa_mcp import install_hint
from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result, TranscriptionError

INSTALL = install_hint("openai-whisper")

_models: dict[tuple[str, str], Any] = {}
_lock = threading.Lock()


class OpenAIWhisper(Backend):
    name = "openai-whisper"

    def __init__(self, model: str, device: str = "auto") -> None:
        self.model_id = model or "turbo"
        self.device = device or "auto"

    def check(self) -> None:
        try:
            import whisper  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailable(f"openai-whisper is not installed: {INSTALL}") from exc

    def _model(self) -> Any:
        self.check()
        import whisper

        key = (self.model_id, self.device)
        with _lock:
            if key not in _models:
                device = None if self.device == "auto" else self.device
                try:
                    _models[key] = whisper.load_model(self.model_id, device=device)
                except Exception as exc:
                    raise BackendUnavailable(f"cannot load model {self.model_id!r}: {exc}") from exc
            return _models[key]

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        import whisper

        model = self._model()
        try:
            audio = whisper.load_audio(str(wav))
            mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(audio), n_mels=model.dims.n_mels).to(
                model.device
            )
            _, probs = model.detect_language(mel)
            detected = max(probs, key=probs.get)
            out = model.transcribe(audio, language=language, condition_on_previous_text=False)
        except Exception as exc:
            raise TranscriptionError(f"openai-whisper failed: {exc}") from exc
        lang = language or str(out.get("language") or detected)
        return Result(str(out.get("text", "")).strip(), lang, float(probs.get(lang, 0.0)))
