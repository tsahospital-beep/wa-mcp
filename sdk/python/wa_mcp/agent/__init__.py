"""Public v1 extension point for reply providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

MAX_AUDIO = 8 * 1024 * 1024
MAX_TEXT = 12000


@dataclass(frozen=True)
class Event:
    account: str
    chat: str
    chat_name: str
    sender: str
    from_name: str
    message_id: str
    text: str = ""
    transcript: str = ""
    media_kind: str = "text"
    history: list[dict[str, str]] = field(default_factory=list)
    language_hint: str = ""
    event: str = "message"

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"] = data.pop("sender")
        return data


@dataclass(frozen=True)
class Reply:
    reply_text: str = ""
    reply_audio_url: str = ""
    reply_audio_base64: str = ""
    audio_mime: str = ""
    voice_note: bool = False
    skip: bool = False

    def __post_init__(self) -> None:
        if not all(
            isinstance(s, str)
            for s in (self.reply_text, self.reply_audio_url, self.reply_audio_base64, self.audio_mime)
        ):
            raise ValueError("invalid reply fields")
        if type(self.skip) is not bool or type(self.voice_note) is not bool:
            raise ValueError("invalid reply flags")
        if len(self.reply_text) > MAX_TEXT or len(self.reply_audio_base64) > 4 * ((MAX_AUDIO + 2) // 3):
            raise ValueError("reply exceeds size limit")
        if self.reply_audio_url and self.reply_audio_base64:
            raise ValueError("choose one audio source")
        if not (self.skip or self.reply_text.strip() or self.reply_audio_base64 or self.reply_audio_url):
            raise ValueError("empty agent reply")


class AgentProvider(Protocol):
    async def reply(self, event: Event) -> Reply:
        """Return one proposed reply. The connector owns delivery."""
        ...
