"""Agent settings shared by the worker, SDK and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Voice:
    provider: str = "none"
    always: bool = False
    url: str = ""
    api_key_env: str = ""
    voice_id: str = ""
    model_id: str = ""
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    mode: str = "draft"
    provider: str = "openai"
    allow_chats: tuple[str, ...] = ()
    deny_chats: tuple[str, ...] = ()
    url: str = ""
    api_key_env: str = ""
    prompt_file: str = ""
    history: int = 10
    timeout_s: int = 30
    cooldown_s: int = 60
    dry_run: bool = False
    follow_up_calls: bool = False
    auto_reject: bool = False
    reject_message: str = "I cannot take the call right now. Please leave a message."
    voice: Voice = field(default_factory=Voice)


def _fields(raw: dict[str, Any], defaults: dict[str, Any], name: str) -> dict[str, Any]:
    from wa_mcp.config import ConfigError

    if not isinstance(raw, dict) or raw.keys() - defaults.keys():
        raise ConfigError(f"invalid {name} settings")
    out = {}
    for key, default in defaults.items():
        value = raw[key]
        if type(value) is not type(default):
            raise ConfigError(f"invalid {name}.{key}")
        if isinstance(value, list):
            if not all(isinstance(s, str) and s.strip() for s in value):
                raise ConfigError(f"{name}.{key} requires nonempty strings")
            value = tuple(value)
        out[key] = value
    return out


def parse(raw: dict[str, Any], defaults: dict[str, Any]) -> Settings:
    from wa_mcp.config import ConfigError

    fields = _fields(raw, defaults, "agent")
    fields["voice"] = Voice(**_fields(fields["voice"], defaults["voice"], "agent.voice"))
    cfg = Settings(**fields)
    if cfg.mode not in ("draft", "approve", "auto"):
        raise ConfigError("invalid agent.mode")
    if cfg.provider not in ("http", "openai", "openai-compatible", "anthropic", "ollama"):
        module, _, cls = cfg.provider.partition(":")
        if not cls.isidentifier() or not all(p.isidentifier() for p in module.split(".")):
            raise ConfigError("invalid agent.provider")
    if cfg.voice.provider not in ("none", "elevenlabs", "http-tts", "command"):
        raise ConfigError("invalid agent.voice.provider")
    if not (0 <= cfg.history <= 100 and 1 <= cfg.timeout_s <= 300 and 0 <= cfg.cooldown_s <= 86400):
        raise ConfigError("invalid agent history, timeout or cooldown")
    if cfg.auto_reject and not (0 < len(cfg.reject_message.strip()) <= 500):
        raise ConfigError("agent.reject_message requires 1 to 500 characters")
    return cfg
