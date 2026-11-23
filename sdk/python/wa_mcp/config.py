"""Load config.json, the same file the bridge reads.

Path: argument, then env WA_MCP_CONFIG, then ./config.json. A missing file means
all defaults. Relative paths are resolved against the folder of the config file.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from wa_mcp import secretfile
from wa_mcp.agent import settings as agent_settings

DEFAULTS: dict[str, Any] = {
    "mcp": {"toolsets": ["core"], "legacy_tool_names": False},
    "store_dir": "store",
    "downloads_dir": "downloads",
    "exports_dir": "exports",
    "session_file": "session.db",
    "api": {"listen": "127.0.0.1:8080", "token": "", "token_file_fallback": False},
    "login": {
        "device_name": "WA MCP",
        "device_platform": "desktop",
        "open_window": True,
        "port": 0,
        "public": {"url": "", "listen": "127.0.0.1:8766"},
    },
    "read_receipts": {"auto_mark_read": False},
    "calls": {"record": True},
    "sending": {
        "enabled": False,
        "require_confirm": True,
        "allow_confirm_flag": False,
        "human_like": True,
        "mark_read_before_reply": True,
        "typing_chars_per_second": 7,
        "typing_min_seconds": 1.2,
        "typing_max_seconds": 20,
        "jitter": 0.3,
        "pause_between_messages_seconds": [1.0, 3.5],
        "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00", "action": "queue"},
        "limits": {
            "per_chat_per_hour": 20,
            "per_hour": 60,
            "per_day": 300,
            "chats_per_identical_message_per_hour": 3,
        },
        "dedupe_window_minutes": 10,
        "allowed_media_dirs": [],
    },
    "webhooks": [],
    "accounts": {},
    "encryption": {"mode": "keystore", "passphrase_env": "WA_MCP_PASSPHRASE", "key_file": "", "efs": True},
    "transcription": {
        "backend": "faster-whisper",
        "model": "large-v3",
        "device": "auto",
        "compute_type": "auto",
        "language": None,
        "chunk_seconds": 30,
        "whisper_cpp": {"binary": "", "model": ""},
        "http": {
            "url": "http://127.0.0.1:8000/v1/audio/transcriptions",
            "model": "whisper-1",
            "api_key_env": "WA_MCP_STT_API_KEY",
        },
        "command": {"argv": []},
    },
    "people": {"aliases": {}},
    "tasks": {"projects": [], "keywords": {}},
    "mentions": {"watch": []},
    "import": {"allowed_dirs": [], "max_mb": 1024, "omitted_phrases": []},
    "agent": json.loads(json.dumps(asdict(agent_settings.Settings()))),
    "ai": {
        "provider": "none",
        "base_url": "",
        "text_model": "",
        "vision_model": "",
        "api_key_env": "WA_MCP_AI_API_KEY",
        "timeout_s": 60,
        "max_calls_per_day": 200,
        "max_input_chars": 12000,
    },
    "vision": {
        "ocr": "tesseract",
        "languages": ["eng"],
        "tesseract_binary": "",
        "max_image_mb": 20,
        "timeout_s": 60,
        "min_text_chars": 3,
    },
    # Remote mode (wa-mcp serve --http); parsed by wa_mcp_server.remote.settings.
    "server": {
        "listen": "127.0.0.1:8765",
        "public_url": "",
        "allowed_hosts": [],
        "allowed_origins": [],
        "cors_origins": [],
        "trusted_proxies": [],
        "max_request_kb": 256,
        "rate_limit": {
            "per_token_per_minute": 120,
            "per_ip_per_minute": 240,
            "auth_per_ip_per_minute": 20,
            "registrations_per_ip_per_hour": 10,
        },
        "oauth": {
            "enabled": False,
            "approval": "link",
            "admin_password_env": "WA_MCP_ADMIN_PASSWORD",
            "access_token_minutes": 60,
            "refresh_token_days": 30,
            "max_clients": 20,
            # Claude connectors and local desktop clients; [] allows any https or loopback address.
            "redirect_uri_prefixes": [
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
                "http://localhost:",
                "http://127.0.0.1:",
                "http://[::1]:",
            ],
        },
    },
}

# Same overrides as the bridge (internal/config).
_ENV_STR = {
    "WA_MCP_STORE_DIR": ("store_dir",),
    "WA_MCP_DOWNLOADS_DIR": ("downloads_dir",),
    "WA_MCP_EXPORTS_DIR": ("exports_dir",),
    "WA_MCP_API_LISTEN": ("api", "listen"),
    "WA_MCP_API_TOKEN": ("api", "token"),
    "WA_MCP_ENCRYPTION_MODE": ("encryption", "mode"),
    "WA_MCP_KEY_FILE": ("encryption", "key_file"),
    # Python only (remote mode).
    "WA_MCP_SERVER_LISTEN": ("server", "listen"),
    "WA_MCP_SERVER_PUBLIC_URL": ("server", "public_url"),
}
_ENV_BOOL = {
    "WA_MCP_SENDING_ENABLED": ("sending", "enabled"),
    "WA_MCP_LOGIN_OPEN_WINDOW": ("login", "open_window"),
}
_LANG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,31}$")
_TRUE = {"1", "t", "T", "TRUE", "true", "True"}
_FALSE = {"0", "f", "F", "FALSE", "false", "False"}
# The account that uses the top-level settings; it always exists (bridge: internal/config/accounts.go).
DEFAULT_ACCOUNT = "default"
_ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ConfigError(ValueError):
    """The config file is unreadable or has a wrong value."""


@dataclass(frozen=True)
class ApiConfig:
    listen: str
    token: str


@dataclass(frozen=True)
class QuietHours:
    enabled: bool
    start: str
    end: str
    action: str


@dataclass(frozen=True)
class Limits:
    per_chat_per_hour: int
    per_hour: int
    per_day: int
    chats_per_identical_message_per_hour: int = 3


@dataclass(frozen=True)
class SendingConfig:
    enabled: bool
    require_confirm: bool
    allow_confirm_flag: bool
    human_like: bool
    mark_read_before_reply: bool
    typing_chars_per_second: float
    typing_min_seconds: float
    typing_max_seconds: float
    jitter: float
    pause_between_messages_seconds: tuple[float, float]
    quiet_hours: QuietHours
    limits: Limits
    dedupe_window_minutes: float
    allowed_media_dirs: tuple[Path, ...]


ENCRYPTION_MODES = ("keystore", "passphrase", "keyfile", "off")


@dataclass(frozen=True)
class EncryptionConfig:
    """Field encryption of wa.db (see wa_mcp.keystore); same rules as bridge/internal/config/encryption.go."""

    mode: str
    passphrase_env: str
    key_file: str
    efs: bool

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


def _encryption_compat(user: dict[str, Any]) -> None:
    """The old switch encryption.enabled=true without a mode means mode passphrase."""
    enc = user.get("encryption")
    if isinstance(enc, dict) and enc.get("enabled") is True and "mode" not in enc:
        enc["mode"] = "passphrase"


def _encryption(enc: dict[str, Any], resolve_opt: Callable[[str], str]) -> EncryptionConfig:
    mode = _str(enc, "mode", "encryption") or "keystore"
    if mode not in ENCRYPTION_MODES:
        raise ConfigError(f"encryption.mode: use one of {', '.join(ENCRYPTION_MODES)}")
    passphrase_env = _str(enc, "passphrase_env", "encryption")
    key_file = resolve_opt(_str(enc, "key_file", "encryption"))
    if mode == "passphrase" and not passphrase_env:
        raise ConfigError("encryption.passphrase_env is empty")
    if mode == "keyfile" and not key_file:
        raise ConfigError("encryption.key_file is empty (needed for mode keyfile)")
    return EncryptionConfig(
        mode=mode, passphrase_env=passphrase_env, key_file=key_file, efs=_bool(enc, "efs", "encryption")
    )


@dataclass(frozen=True)
class TranscriptionConfig:
    backend: str
    model: str
    device: str
    compute_type: str
    language: str | None
    chunk_seconds: float
    whisper_cpp_binary: str
    whisper_cpp_model: str
    http_url: str
    http_model: str
    http_api_key_env: str
    command_argv: tuple[str, ...]


AI_PROVIDERS = ("none", "ollama", "openai-compatible", "anthropic")
OCR_ENGINES = ("tesseract", "rapidocr", "none")


@dataclass(frozen=True)
class AiConfig:
    provider: str
    base_url: str
    text_model: str
    vision_model: str
    api_key_env: str
    timeout_s: float
    max_calls_per_day: int
    max_input_chars: int


@dataclass(frozen=True)
class VisionConfig:
    ocr: str
    languages: tuple[str, ...]
    tesseract_binary: str
    max_image_mb: float
    timeout_s: float
    min_text_chars: int


@dataclass(frozen=True)
class Config:
    file: Path
    dir: Path
    store_dir: Path
    downloads_dir: Path
    exports_dir: Path
    api: ApiConfig
    sending: SendingConfig
    encryption: EncryptionConfig
    transcription: TranscriptionConfig
    ai: AiConfig
    vision: VisionConfig
    aliases: dict[str, str]
    auto_mark_read: bool
    raw: dict[str, Any] = field(repr=False)
    # Several WhatsApp numbers: see for_account and docs/guides/accounts.md.
    account: str = "default"
    account_names: tuple[str, ...] = ("default",)
    top_raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    agent: agent_settings.Settings = field(default_factory=agent_settings.Settings)
    demo: bool = False  # In-memory only. A bridge configuration cannot select the demo store.

    def for_account(self, name: str | None) -> Config:
        """The settings of another account of the same config file (None or "" = "default")."""
        if (name or DEFAULT_ACCOUNT) == self.account:
            return self
        return _account_config(self.file, self.top_raw or self.raw, name)

    @property
    def wa_db(self) -> Path:
        return self.store_dir / "wa.db"

    @property
    def session_db(self) -> Path:
        """The whatsmeow session database: session_file inside store_dir, or its absolute path."""
        return self.store_dir / session_file(self.raw if "session_file" in self.raw else self.top_raw)

    @property
    def insight_db(self) -> Path:
        """Python-owned state (sync progress, task tracker)."""
        return self.store_dir / "insight.db"

    @property
    def token_file(self) -> Path:
        return self.store_dir / "api.token"

    def section(self, name: str) -> dict[str, Any]:
        """Raw dict of a top-level section, for modules with their own keys (tasks, mentions)."""
        value = self.raw.get(name)
        return dict(value) if isinstance(value, dict) else {}


def secret_env(name: str) -> str:
    """Value of the variable `name`, or else the content of the file named by `name`_FILE (Docker
    secrets; checked like a key file by wa_mcp.secretfile). Surrounding white space is removed."""
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value:
        return value.strip()
    path = os.environ.get(name + "_FILE", "")
    if not path:
        return ""
    try:
        return secretfile.read(path).decode("utf-8", "replace").strip()
    except secretfile.SecretFileError as exc:
        raise ConfigError(f"{name}_FILE: {exc}") from exc


def expand_home(value: str) -> str:
    """A leading "~" alone or before a separator becomes the home folder (same rule as the bridge)."""
    if value != "~" and not value.startswith(("~/", "~\\")):
        return value
    home = os.path.expanduser("~")
    return home if value == "~" else os.path.join(home, value[2:])


def resolve_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("WA_MCP_CONFIG")
    if env:
        return Path(env)
    return Path("config.json")


def load(path: str | os.PathLike[str] | None = None, account: str | None = None) -> Config:
    """Read the config file (missing file = defaults) and apply env overrides.

    account picks an entry of "accounts" (default: the account "default", the top-level settings).
    """
    file = resolve_path(path).resolve()
    data = copy.deepcopy(DEFAULTS)
    if file.is_file():
        try:
            user = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"config {file}: {exc}") from exc
        if not isinstance(user, dict):
            raise ConfigError(f"config {file}: top level must be an object")
        _encryption_compat(user)
        _merge(data, user)
    _apply_env(data)
    return _account_config(file, data, account)


def _merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def isolated_defaults(directory: Path, overrides: dict[str, Any]) -> Config:
    """Build a configuration without reading host files, secrets or environment overrides."""
    data = copy.deepcopy(DEFAULTS)
    _merge(data, overrides)
    return _account_config(directory.resolve() / "config.json", data, None)


def _apply_env(data: dict[str, Any]) -> None:
    for name, keys in _ENV_STR.items():
        value = os.environ.get(name)
        if value:
            _set(data, keys, value)
    for name, keys in _ENV_BOOL.items():
        value = os.environ.get(name)
        if not value:
            continue
        if value in _TRUE:
            _set(data, keys, True)
        elif value in _FALSE:
            _set(data, keys, False)
        else:
            raise ConfigError(f"{name}: want true or false, got {value!r}")


def _set(data: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    node = data
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _section(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    node: Any = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        raise ConfigError(f"{'.'.join(keys)}: must be an object")
    return node


def _str(node: dict[str, Any], key: str, where: str) -> str:
    value = node.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: must be a string")
    return value


def _bool(node: dict[str, Any], key: str, where: str) -> bool:
    value = node.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: must be true or false")
    return value


def _num(node: dict[str, Any], key: str, where: str) -> float:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}.{key}: must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ConfigError(f"{where}.{key}: must be finite") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{where}.{key}: must be finite")
    return number


def _positive(value: float, where: str) -> float:
    if value <= 0:
        raise ConfigError(f"{where}: must be greater than 0")
    return value


def _count(value: float, where: str, lowest: int) -> int:
    if value != int(value) or value < lowest:
        raise ConfigError(f"{where}: must be a whole number of at least {lowest}")
    return int(value)


_RESERVED_STORE_FILES = ("wa.db", "insight.db", "api.token", "bridge.lock")


def session_file(data: dict[str, Any]) -> str:
    """session_file of a config dict: a plain file name inside store_dir, or an absolute path such as a
    volume only the bridge mounts (same rules as the bridge, which also refuses send folders)."""
    value = data.get("session_file")
    if value is None or (isinstance(value, str) and not value.strip()):
        return "session.db"
    name = value.strip() if isinstance(value, str) else ""
    if name and os.path.isabs(expand_home(name)):
        return os.path.normpath(expand_home(name))
    if not name or name in (".", "..") or any(c in name for c in "/\\:"):
        raise ConfigError(f"session_file: want a file name inside store_dir, got {value!r}")
    if name.lower() in _RESERVED_STORE_FILES:
        raise ConfigError(f"session_file: {name!r} is used by the bridge itself")
    return name


def _build(file: Path, data: dict[str, Any]) -> Config:
    base = file.parent
    session_file(data)

    def resolve(value: str) -> Path:
        p = Path(expand_home(value))
        return p if p.is_absolute() else (base / p)

    def resolve_opt(value: str) -> str:
        return str(resolve(value)) if value.strip() else ""

    store = _str(data, "store_dir", "config") or "store"
    login = _section(data, "login")
    name = _str(login, "device_name", "login")
    if not name.strip() or "\r" in name or "\n" in name:
        raise ConfigError("login.device_name must be a non-empty single line")
    if _str(login, "device_platform", "login") not in (
        "desktop",
        "chrome",
        "firefox",
        "safari",
        "edge",
        "unknown",
    ):
        raise ConfigError("invalid login.device_platform")
    api = _section(data, "api")
    snd = _section(data, "sending")
    qh = _section(data, "sending", "quiet_hours")
    lim = _section(data, "sending", "limits")
    for limit in ("per_chat_per_hour", "per_hour", "per_day", "chats_per_identical_message_per_hour"):
        value = lim.get(limit)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"sending.limits.{limit}: must be a nonnegative integer")
    enc = _section(data, "encryption")
    tr = _section(data, "transcription")
    wcpp = _section(data, "transcription", "whisper_cpp")
    http = _section(data, "transcription", "http")
    cmd = _section(data, "transcription", "command")
    people = _section(data, "people")
    receipts = _section(data, "read_receipts")
    ai = _section(data, "ai")
    vis = _section(data, "vision")

    pause = snd.get("pause_between_messages_seconds")
    if not (isinstance(pause, list) and len(pause) == 2 and all(isinstance(x, int | float) for x in pause)):
        raise ConfigError("sending.pause_between_messages_seconds: need [min, max]")
    dirs = snd.get("allowed_media_dirs") or []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise ConfigError("sending.allowed_media_dirs: must be a list of paths")
    argv = cmd.get("argv") or []
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ConfigError("transcription.command.argv: must be a list of strings")
    aliases = people.get("aliases") or {}
    if not isinstance(aliases, dict) or not all(isinstance(v, str) for v in aliases.values()):
        raise ConfigError("people.aliases: must map a phone or JID to a name")
    language = tr.get("language")
    if language is not None and not isinstance(language, str):
        raise ConfigError("transcription.language: must be a language code or null")

    provider = (_str(ai, "provider", "ai") or "none").lower().replace("_", "-")
    if provider not in AI_PROVIDERS:
        raise ConfigError(f"ai.provider: use one of {', '.join(AI_PROVIDERS)}")
    base_url = _str(ai, "base_url", "ai").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ConfigError("ai.base_url: must start with http:// or https://")
    ocr = (_str(vis, "ocr", "vision") or "none").lower()
    if ocr not in OCR_ENGINES:
        raise ConfigError(f"vision.ocr: use one of {', '.join(OCR_ENGINES)}")
    langs = vis.get("languages")
    if not isinstance(langs, list) or not all(isinstance(x, str) and _LANG.match(x) for x in langs):
        raise ConfigError('vision.languages: must be a list of language codes such as ["eng"]')

    return Config(
        file=file,
        dir=base,
        store_dir=resolve(store),
        downloads_dir=resolve(_str(data, "downloads_dir", "config") or "downloads"),
        exports_dir=resolve(_str(data, "exports_dir", "config") or "exports"),
        api=ApiConfig(listen=_str(api, "listen", "api"), token=_str(api, "token", "api").strip()),
        sending=SendingConfig(
            enabled=_bool(snd, "enabled", "sending"),
            require_confirm=_bool(snd, "require_confirm", "sending"),
            allow_confirm_flag=_bool(snd, "allow_confirm_flag", "sending"),
            human_like=_bool(snd, "human_like", "sending"),
            mark_read_before_reply=_bool(snd, "mark_read_before_reply", "sending"),
            typing_chars_per_second=_num(snd, "typing_chars_per_second", "sending"),
            typing_min_seconds=_num(snd, "typing_min_seconds", "sending"),
            typing_max_seconds=_num(snd, "typing_max_seconds", "sending"),
            jitter=_num(snd, "jitter", "sending"),
            pause_between_messages_seconds=(float(pause[0]), float(pause[1])),
            quiet_hours=QuietHours(
                enabled=_bool(qh, "enabled", "sending.quiet_hours"),
                start=_str(qh, "start", "sending.quiet_hours"),
                end=_str(qh, "end", "sending.quiet_hours"),
                action=_str(qh, "action", "sending.quiet_hours") or "queue",
            ),
            limits=Limits(
                per_chat_per_hour=lim["per_chat_per_hour"],
                per_hour=lim["per_hour"],
                per_day=lim["per_day"],
                chats_per_identical_message_per_hour=lim["chats_per_identical_message_per_hour"],
            ),
            dedupe_window_minutes=_num(snd, "dedupe_window_minutes", "sending"),
            allowed_media_dirs=tuple(resolve(d) for d in dirs if d.strip()),
        ),
        encryption=_encryption(enc, resolve_opt),
        transcription=TranscriptionConfig(
            backend=_str(tr, "backend", "transcription") or "faster-whisper",
            model=_str(tr, "model", "transcription"),
            device=_str(tr, "device", "transcription") or "auto",
            compute_type=_str(tr, "compute_type", "transcription") or "auto",
            language=(language or None) if language != "auto" else None,
            chunk_seconds=_num(tr, "chunk_seconds", "transcription"),
            whisper_cpp_binary=resolve_opt(_str(wcpp, "binary", "transcription.whisper_cpp")),
            whisper_cpp_model=resolve_opt(_str(wcpp, "model", "transcription.whisper_cpp")),
            http_url=_str(http, "url", "transcription.http"),
            http_model=_str(http, "model", "transcription.http"),
            http_api_key_env=_str(http, "api_key_env", "transcription.http"),
            command_argv=tuple(argv),
        ),
        ai=AiConfig(
            provider=provider,
            base_url=base_url.rstrip("/"),
            text_model=_str(ai, "text_model", "ai").strip(),
            vision_model=_str(ai, "vision_model", "ai").strip(),
            api_key_env=_str(ai, "api_key_env", "ai").strip(),
            timeout_s=_positive(_num(ai, "timeout_s", "ai"), "ai.timeout_s"),
            max_calls_per_day=_count(_num(ai, "max_calls_per_day", "ai"), "ai.max_calls_per_day", 0),
            max_input_chars=_count(_num(ai, "max_input_chars", "ai"), "ai.max_input_chars", 1),
        ),
        agent=agent_settings.parse(_section(data, "agent"), DEFAULTS["agent"]),
        vision=VisionConfig(
            ocr=ocr,
            languages=tuple(langs),
            tesseract_binary=resolve_opt(_str(vis, "tesseract_binary", "vision")),
            max_image_mb=_positive(_num(vis, "max_image_mb", "vision"), "vision.max_image_mb"),
            timeout_s=_positive(_num(vis, "timeout_s", "vision"), "vision.timeout_s"),
            min_text_chars=_count(_num(vis, "min_text_chars", "vision"), "vision.min_text_chars", 0),
        ),
        aliases={str(k): v for k, v in aliases.items()},
        auto_mark_read=_bool(receipts, "auto_mark_read", "read_receipts"),
        raw=data,
    )


# Accounts: one bridge per WhatsApp number, each with its own store and API address. The rules match the
# bridge (internal/config/accounts.go): "default" uses the top-level settings; another account gets
# <folder>/<name> for store_dir, downloads_dir and exports_dir, needs its own api.listen, and its block
# may set those keys, login.port and anything under sending.


def _account_blocks(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    blocks = data.get("accounts")
    if blocks is None:
        return {}
    if not isinstance(blocks, dict):
        raise ConfigError("accounts: must map account names to objects")
    out: dict[str, dict[str, Any]] = {}
    for name, block in blocks.items():
        if not _ACCOUNT_NAME.match(str(name)):
            raise ConfigError(
                f'accounts: "{name}" is not a valid account name (use a-z, 0-9, - and _, at most 32 characters)'
            )
        if block is not None and not isinstance(block, dict):
            raise ConfigError(f"accounts.{name}: must be an object")
        out[str(name)] = block or {}
    return out


def _account_data(data: dict[str, Any], name: str, blocks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    if name != DEFAULT_ACCOUNT:
        for key in ("store_dir", "downloads_dir", "exports_dir"):
            out[key] = os.path.join(_str(data, key, "config"), name)
        if isinstance(out.get("api"), dict):
            out["api"]["listen"] = ""  # two bridges cannot share one address
    block = blocks.get(name, {})
    for key in ("store_dir", "downloads_dir", "exports_dir"):
        if key in block:
            out[key] = block[key]
    for section, key in (("api", "listen"), ("login", "port"), ("sending", "")):
        sub = block.get(section)
        if sub is None:
            continue
        if not isinstance(sub, dict) or not isinstance(out.get(section), dict):
            raise ConfigError(f"accounts.{name}.{section}: must be an object")
        if not key:
            _merge(out[section], copy.deepcopy(sub))
        elif key in sub:
            out[section][key] = sub[key]
    return out


def _check_accounts(
    file: Path, data: dict[str, Any], blocks: dict[str, dict[str, Any]], names: tuple[str, ...]
) -> None:
    """Every account needs its own store folder, API port and login port (0 = any free port)."""
    if len(names) < 2:
        return
    ports: dict[str, str] = {}
    stores: dict[str, str] = {}
    logins: dict[object, str] = {}
    for n in names:
        d = _account_data(data, n, blocks)
        listen = d["api"].get("listen") if isinstance(d.get("api"), dict) else None
        if not listen or not isinstance(listen, str):
            raise ConfigError(
                f"accounts.{n}.api.listen: set an address for this account, for example 127.0.0.1:8081"
            )
        port = listen.rpartition(":")[2]
        if port in ports and port != "0":
            raise ConfigError(
                f"accounts.{n}.api.listen: port {port} is already used by account {ports[port]}"
            )
        ports[port] = n
        p = Path(expand_home(_str(d, "store_dir", f"accounts.{n}")))
        store = os.path.normpath(p if p.is_absolute() else file.parent / p)
        if os.path.normcase(store) in stores:
            raise ConfigError(
                f"accounts.{n}.store_dir: {store} is already used by account {stores[os.path.normcase(store)]}; "
                "every account needs its own store folder"
            )
        stores[os.path.normcase(store)] = n
        login = d["login"].get("port", 0) if isinstance(d.get("login"), dict) else 0
        if login and login in logins:
            raise ConfigError(
                f"accounts.{n}.login.port: {login} is already used by account {logins[login]}; use another port or 0"
            )
        logins[login] = n


def _account_config(file: Path, data: dict[str, Any], account: str | None) -> Config:
    blocks = _account_blocks(data)
    names = (DEFAULT_ACCOUNT, *sorted(n for n in blocks if n != DEFAULT_ACCOUNT))
    name = account or DEFAULT_ACCOUNT
    if name not in names:
        raise ConfigError(f'account "{name}" is not configured (accounts: {", ".join(names)})')
    _check_accounts(file, data, blocks, names)
    view = _account_data(data, name, blocks) if blocks else data
    return replace(_build(file, view), account=name, account_names=names, top_raw=data)
