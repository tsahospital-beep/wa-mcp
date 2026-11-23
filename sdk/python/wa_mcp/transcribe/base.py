"""Backend interface for speech to text."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class BackendUnavailable(Exception):
    """The backend is not installed or not configured; the message says how to fix it."""


class TranscriptionError(Exception):
    """The backend ran but failed on this file."""


@dataclass
class Result:
    text: str
    language: str | None = None
    confidence: float | None = None  # probability of the language, when the backend reports it
    hinted: bool = False  # language taken from the chat's history instead of detection


class Backend(ABC):
    """Turns a 16 kHz mono WAV file into text."""

    name = "base"

    @abstractmethod
    def check(self) -> None:
        """Raise BackendUnavailable when the backend cannot run."""

    @abstractmethod
    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        """language None means detect."""


# Whisper language names returned by OpenAI-compatible servers, mapped to ISO 639-1.
LANGUAGE_CODES = {
    "afrikaans": "af", "albanian": "sq", "amharic": "am", "arabic": "ar", "armenian": "hy",
    "azerbaijani": "az", "basque": "eu", "belarusian": "be", "bengali": "bn", "bosnian": "bs",
    "bulgarian": "bg", "catalan": "ca", "chinese": "zh", "croatian": "hr", "czech": "cs", "danish": "da",
    "dutch": "nl", "english": "en", "estonian": "et", "finnish": "fi", "french": "fr", "galician": "gl",
    "georgian": "ka", "german": "de", "greek": "el", "gujarati": "gu", "hebrew": "he", "hindi": "hi",
    "hungarian": "hu", "icelandic": "is", "indonesian": "id", "italian": "it", "japanese": "ja",
    "kannada": "kn", "kazakh": "kk", "korean": "ko", "latvian": "lv", "lithuanian": "lt",
    "macedonian": "mk", "malay": "ms", "malayalam": "ml", "marathi": "mr", "mongolian": "mn",
    "nepali": "ne", "norwegian": "no", "persian": "fa", "polish": "pl", "portuguese": "pt",
    "punjabi": "pa", "romanian": "ro", "russian": "ru", "serbian": "sr", "slovak": "sk",
    "slovenian": "sl", "spanish": "es", "swahili": "sw", "swedish": "sv", "tagalog": "tl", "tamil": "ta",
    "telugu": "te", "thai": "th", "turkish": "tr", "ukrainian": "uk", "urdu": "ur", "uzbek": "uz",
    "vietnamese": "vi", "welsh": "cy",
}  # fmt: skip


def normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    return LANGUAGE_CODES.get(v, v)
