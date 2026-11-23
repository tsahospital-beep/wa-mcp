"""Ollama (https://ollama.com) through its /api/chat endpoint. Local by default."""

from __future__ import annotations

from typing import Any

from wa_mcp.ai.base import AIError, Provider, b64


class Ollama(Provider):
    name = "ollama"
    default_url = "http://127.0.0.1:11434"

    def _chat(self, model: str, messages: list[dict[str, Any]], max_tokens: int) -> str:
        headers = {"Authorization": f"Bearer {self._key()}"} if self._key() else {}
        payload = {
            "model": model,
            "stream": False,
            "messages": messages,
            "options": {"num_predict": max_tokens},
        }
        body = self._post(f"{self._url()}/api/chat", headers, payload)
        message = body.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise AIError("the AI provider sent an answer without text")
        return content

    def _complete(self, model: str, system: str, prompt: str, max_tokens: int) -> str:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._chat(model, messages, max_tokens)

    def _describe(
        self, model: str, system: str, prompt: str, image: bytes, media_type: str, max_tokens: int
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt, "images": [b64(image)]},
        ]
        return self._chat(model, messages, max_tokens)
