"""Anthropic Messages API over plain HTTP. The key is read from the environment only."""

from __future__ import annotations

from typing import Any

from wa_mcp.ai.base import AIError, Provider, b64

API_VERSION = "2023-06-01"


class Anthropic(Provider):
    name = "anthropic"
    default_url = "https://api.anthropic.com"
    needs_key = True

    def _messages(self, model: str, system: str, content: Any, max_tokens: int) -> str:
        base = self._url()
        url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        headers = {"x-api-key": self._key(), "anthropic-version": API_VERSION}
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system
        body = self._post(url, headers, payload)
        if body.get("stop_reason") == "refusal":
            raise AIError("the AI provider declined the request")
        blocks = body.get("content")
        texts = (
            [str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            if isinstance(blocks, list)
            else []
        )
        text = "".join(texts)
        if not text.strip() and body.get("stop_reason") == "max_tokens":
            raise AIError(
                "the answer was cut off before any text; the model may spend its limit on reasoning"
            )
        return text

    def _complete(self, model: str, system: str, prompt: str, max_tokens: int) -> str:
        return self._messages(model, system, prompt, max_tokens)

    def _describe(
        self, model: str, system: str, prompt: str, image: bytes, media_type: str, max_tokens: int
    ) -> str:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64(image)}},
            {"type": "text", "text": prompt},
        ]
        return self._messages(model, system, content, max_tokens)
