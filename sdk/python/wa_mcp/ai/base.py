"""Provider interface, errors and the guards every call passes through.

A provider turns a text prompt (or a prompt plus an image) into text. The base class owns the parts that
must not differ between vendors: the input size limit, the daily call counter, reading the image file,
the API key lookup (environment only) and the HTTP error messages. A vendor class only builds the
request and reads the answer.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from wa_mcp import config as config_mod
from wa_mcp.ai import prompts

DEFAULT_MAX_TOKENS = 400
MAX_MAX_TOKENS = 4096
ERROR_DETAIL_CHARS = 200
# Largest answer read from a provider; a longer one is refused.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class AIError(Exception):
    """A provider call failed. The message is safe to show: it never holds a key or the prompt."""


class AIUnavailable(AIError):
    """No provider is configured, or its settings are incomplete."""


class AILimitError(AIError):
    """A cost guard (input size or calls per day) stopped the call before anything was sent."""


Meter = Callable[[], None]


def image_media_type(head: bytes) -> str | None:
    """Media type from the first bytes of an image file; None when it is not a supported format."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_image(path: str | Path, max_bytes: int) -> tuple[bytes, str]:
    """Bytes and media type of an image file, with a size limit."""
    p = Path(path)
    if not p.is_file():
        raise AIError(f"no such image file: {p.name}")
    size = p.stat().st_size
    if size > max_bytes:
        raise AIError(
            f"image {p.name} is {size / 1_048_576:.1f} MB, above the limit of {max_bytes / 1_048_576:.0f} MB"
        )
    data = p.read_bytes()
    media_type = image_media_type(data[:16])
    if media_type is None:
        raise AIError(f"image {p.name} is not a JPEG, PNG, GIF or WebP file")
    return data, media_type


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Provider(ABC):
    """complete() and describe_image() are the whole interface. Subclasses implement _complete/_describe."""

    name = "base"
    default_url = ""
    needs_key = False

    def __init__(
        self,
        cfg: config_mod.AiConfig,
        meter: Meter | None = None,
        transport: httpx.BaseTransport | None = None,
        max_image_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.cfg = cfg
        self.meter = meter
        self.transport = transport
        self.max_image_bytes = max_image_bytes

    # public interface

    def complete(self, prompt: str, system: str = "", max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        """Answer a prompt with the text model. Untrusted content in the prompt must be wrapped by the caller."""
        model = self._model(self.cfg.text_model, "ai.text_model")
        self._guard(len(prompt) + len(system))
        text = self._complete(model, system, prompt, _tokens(max_tokens))
        return _answer(text)

    def describe_image(self, path: str | Path, prompt: str) -> str:
        """Describe an image file with the vision model."""
        model = self._model(self.cfg.vision_model, "ai.vision_model")
        data, media_type = read_image(path, self.max_image_bytes)
        self._guard(len(prompt) + len(prompts.IMAGE_SYSTEM))
        text = self._describe(model, prompts.IMAGE_SYSTEM, prompt, data, media_type, 400)
        return _answer(text)

    def check(self) -> None:
        """Raise AIUnavailable when the settings cannot work."""
        self._url()
        if self.needs_key and not self._key():
            raise AIUnavailable(
                f"set the environment variable {self.cfg.api_key_env or 'named by ai.api_key_env'} to the API key"
            )

    # vendor hooks

    @abstractmethod
    def _complete(self, model: str, system: str, prompt: str, max_tokens: int) -> str: ...

    @abstractmethod
    def _describe(
        self, model: str, system: str, prompt: str, image: bytes, media_type: str, max_tokens: int
    ) -> str: ...

    # shared helpers

    def _model(self, value: str, key: str) -> str:
        if not value:
            raise AIUnavailable(f"set {key} to the model this provider should use")
        return value

    def _url(self) -> str:
        url = self.cfg.base_url or self.default_url
        if not url:
            raise AIUnavailable("set ai.base_url to the address of the provider")
        u = urlsplit(url)
        if u.scheme == "http" and not _loopback(u.hostname or ""):
            raise AIUnavailable(
                "ai.base_url must use https for a provider on another machine; plain http is allowed only for "
                "a loopback address such as http://127.0.0.1:11434"
            )
        return url

    def _key(self) -> str:
        return os.environ.get(self.cfg.api_key_env, "").strip() if self.cfg.api_key_env else ""

    def _guard(self, size: int) -> None:
        """Input size and daily count, checked before anything is sent."""
        limit = self.cfg.max_input_chars
        if size > limit:
            raise AILimitError(f"the input is {size} characters, above ai.max_input_chars ({limit})")
        if self.meter is not None:
            self.meter()

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with (
                httpx.Client(timeout=self.cfg.timeout_s, transport=self.transport, trust_env=False) as client,
                client.stream(
                    "POST", url, headers={"Content-Type": "application/json", **headers}, json=payload
                ) as stream,
            ):
                chunks: list[bytes] = []
                size = 0
                for chunk in stream.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AIError(
                            f"the AI provider sent more than {MAX_RESPONSE_BYTES // 1_048_576} MB; "
                            "the answer is refused"
                        )
                    chunks.append(chunk)
                resp = httpx.Response(
                    stream.status_code,
                    headers=stream.headers,
                    content=b"".join(chunks),
                    request=stream.request,
                )
        except httpx.TimeoutException as exc:
            raise AIError(f"the AI provider did not answer within {self.cfg.timeout_s:g} seconds") from exc
        except httpx.HTTPError as exc:
            raise AIError(
                f"the AI provider is not reachable at {_where(url)}: {exc.__class__.__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise AIError(self._http_error(resp))
        try:
            body = resp.json()
        except ValueError as exc:
            raise AIError("the AI provider sent an answer that is not JSON") from exc
        if not isinstance(body, dict):
            raise AIError("the AI provider sent an unexpected answer")
        return body

    def _http_error(self, resp: httpx.Response) -> str:
        status = resp.status_code
        detail = _detail(resp)
        if self._key():
            detail = detail.replace(self._key(), "[key]")
        if status in (401, 403):
            env = self.cfg.api_key_env or "ai.api_key_env"
            text = f"the AI provider refused the credentials (status {status}); check the key in the environment variable {env}"
        elif status == 404:
            text = "the AI provider answered 404: check ai.base_url and that the model exists"
        elif status == 413:
            text = "the AI provider says the request is too large"
        elif status == 429:
            text = "the AI provider says the rate limit or quota is used up (status 429)"
        else:
            text = f"the AI provider answered {status}"
        return f"{text}: {detail}" if detail else text


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _where(url: str) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}" + (f":{parsed.port}" if parsed.port else "")


def _detail(resp: httpx.Response) -> str:
    """The provider's own error text, short and without control characters."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        err = err.get("message")
    if not isinstance(err, str):
        return ""
    return " ".join(_CONTROL.sub(" ", err).split())[:ERROR_DETAIL_CHARS]


def _tokens(value: int) -> int:
    return max(16, min(int(value), MAX_MAX_TOKENS))


def _answer(text: str) -> str:
    out = text.strip()
    if not out:
        raise AIError("the AI provider returned an empty answer")
    return out
