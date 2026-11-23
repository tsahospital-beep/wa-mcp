"""OpenAI-compatible /v1/audio/transcriptions backend.

The API key is read only from the environment variable named in
transcription.http.api_key_env; it is never stored or logged.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result, TranscriptionError, normalize_language


class HttpBackend(Backend):
    name = "http"

    def __init__(
        self,
        url: str,
        model: str,
        api_key_env: str = "",
        timeout: float = 600.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.transport = transport

    def check(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise BackendUnavailable(
                "set transcription.http.url to an OpenAI-compatible transcription endpoint"
            )

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        self.check()
        headers = {}
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = {"model": self.model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        try:
            with (
                httpx.Client(timeout=self.timeout, transport=self.transport, trust_env=False) as client,
                wav.open("rb") as fh,
            ):
                resp = client.post(
                    self.url, headers=headers, data=data, files={"file": (wav.name, fh, "audio/wav")}
                )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"transcription server not reachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise TranscriptionError(f"transcription server answered {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return Result(resp.text.strip(), language, None)
        lang = normalize_language(body.get("language")) or language
        prob = body.get("language_probability")
        return Result(str(body.get("text", "")).strip(), lang, float(prob) if prob is not None else None)
