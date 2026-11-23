"""deploy/docker without Docker: the values that compose would resolve from env.example, and that CI runs
the documented steps. CI then builds the images and starts the stack (job "docker")."""

from __future__ import annotations

import json
import re
from pathlib import Path

DOCKER = Path(__file__).resolve().parents[2] / "deploy" / "docker"
CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_VAR = re.compile(r"\$\{(\w+)(?::?-([^}]*)|:\?[^}]*)?\}")


def _env_example() -> dict[str, str]:
    out = {}
    for line in (DOCKER / "env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _resolve(text: str, env: dict[str, str]) -> str:
    """${VAR:-default} and ${VAR:?message} as compose resolves them (empty counts as unset for :-)."""
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)


def _service(compose: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w[\w-]*:\n|^\S)", compose, re.M | re.S)
    assert match, name
    return match.group(1)


def test_env_example_gives_the_key_file_mode() -> None:
    env = _env_example()
    assert env.get("WA_MCP_ENCRYPTION_MODE", "") in ("", "keyfile")
    assert env.get("TZ")
    compose = _resolve((DOCKER / "compose.yaml").read_text(encoding="utf-8"), env)
    for name in ("bridge", "server"):
        svc = _service(compose, name)
        assert "WA_MCP_ENCRYPTION_MODE: keyfile" in svc, name
        assert "WA_MCP_KEY_FILE: /run/secrets/wa_mcp_key" in svc, name
        assert f"TZ: {env['TZ']}" in svc, name
        assert "- wa_mcp_key" in svc, name
    assert "file: ./secrets/wa_mcp.key" in compose
    # session.db lives in a volume only the bridge mounts.
    assert "wa-session:/session" in _service(compose, "bridge")
    assert "wa-session" not in _service(compose, "server")
    preview = (DOCKER / "compose.preview.yaml").read_text(encoding="utf-8")
    assert "secrets" not in preview.split("services:", 1)[1] and "ENCRYPTION_MODE" not in preview


def test_container_config_matches_compose() -> None:
    cfg = json.loads((DOCKER / "config.container.json").read_text(encoding="utf-8"))
    compose = (DOCKER / "compose.yaml").read_text(encoding="utf-8")
    subnet = re.search(r"- subnet: (\S+)", compose)
    assert subnet and cfg["server"]["trusted_proxies"] == [subnet.group(1)]
    assert cfg["session_file"] == "/session/session.db" and cfg["store_dir"].startswith("/data/")
    key_script = (DOCKER / "secrets" / "make-key.sh").read_text(encoding="utf-8")
    assert "wa_mcp.key" in key_script and "65532" in key_script


def test_ci_runs_the_documented_steps() -> None:
    ci = CI.read_text(encoding="utf-8")
    job = ci[ci.index("  docker:\n") :]
    steps = [
        job.index(s)
        for s in (
            "cp env.example .env",
            "sh secrets/make-key.sh",
            "compose.preview.yaml config",
            "up -d --wait",
        )
    ]
    assert steps == sorted(steps)
    assert "compose.preview.yaml port bridge 8765" in job
    assert '"http://$endpoint/health"' in job
    assert "WA_MCP_ENCRYPTION_MODE: keyfile" in job
    assert "uvx pip-audit==" in ci
