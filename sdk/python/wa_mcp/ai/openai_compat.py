"""Any server that implements the OpenAI chat completions API (OpenAI, LM Studio, llama.cpp, vLLM, ...)."""

from __future__ import annotations

from typing import Any

from wa_mcp.ai.base import AIError, Provider, b64


class OpenAICompatible(Provider):
    name = "openai-compatible"

    def _chat(self, model: str, messages: list[dict[str, Any]], max_tokens: int) -> str:
        headers = {"Authorization": f"Bearer {self._key()}"} if self._key() else {}
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        body = self._post(f"{self._url()}/chat/completions", headers, payload)
        choices = body.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
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
        url = f"data:{media_type};base64,{b64(image)}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ]
        return self._chat(model, messages, max_tokens)
