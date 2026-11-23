"""Optional AI provider for background work: summaries and image descriptions.

The default is no provider: the assistant that talks to this server does the thinking. When
ai.provider is set, a few features (my_day summary=true, read_images describe=true) call that provider.
Nothing here runs unless one of those features is asked for.
"""

from __future__ import annotations

import httpx

from wa_mcp import config as config_mod
from wa_mcp import db
from wa_mcp.ai import usage
from wa_mcp.ai.anthropic import Anthropic
from wa_mcp.ai.base import AIError, AILimitError, AIUnavailable, Provider
from wa_mcp.ai.ollama import Ollama
from wa_mcp.ai.openai_compat import OpenAICompatible

__all__ = [
    "AIError",
    "AILimitError",
    "AIUnavailable",
    "Provider",
    "configured",
    "get_provider",
]

NOT_CONFIGURED = "no AI provider configured; your assistant can summarize this"

_PROVIDERS: dict[str, type[Provider]] = {
    "ollama": Ollama,
    "openai-compatible": OpenAICompatible,
    "anthropic": Anthropic,
}


def configured(cfg: config_mod.Config | None = None) -> bool:
    return (cfg or db.store().cfg).ai.provider != "none"


def get_provider(
    cfg: config_mod.Config | None = None,
    transport: httpx.BaseTransport | None = None,
    counted: bool = True,
    credential: str | None = None,
) -> Provider:
    """The configured provider; AIUnavailable when ai.provider is none or the settings are incomplete.

    counted=False leaves out the daily call counter (tests that have no store).
    """
    c = cfg or db.store().cfg
    cls = _PROVIDERS.get(c.ai.provider)
    if cls is None:
        raise AIUnavailable("no AI provider configured: set ai.provider in config.json")
    provider = cls(
        c.ai,
        credential=credential,
        meter=usage.meter(c.ai.max_calls_per_day) if counted else None,
        transport=transport,
        max_image_bytes=int(c.vision.max_image_mb * 1024 * 1024),
    )
    provider.check()
    return provider
