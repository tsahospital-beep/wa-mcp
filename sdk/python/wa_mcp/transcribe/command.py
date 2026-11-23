"""Run any local program: argv template with {file} and {language}; text from stdout. No shell."""

from __future__ import annotations

import subprocess
from pathlib import Path

from wa_mcp.transcribe.base import Backend, BackendUnavailable, Result, TranscriptionError


class CommandBackend(Backend):
    name = "command"

    def __init__(self, argv: tuple[str, ...] | list[str], timeout: float = 1800.0) -> None:
        self.argv = list(argv)
        self.timeout = timeout

    def check(self) -> None:
        if not self.argv or not any("{file}" in a for a in self.argv):
            raise BackendUnavailable(
                'set transcription.command.argv, for example ["my-stt", "--input", "{file}"]'
            )

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        self.check()
        argv = [a.replace("{file}", str(wav)).replace("{language}", language or "auto") for a in self.argv]
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TranscriptionError(f"command did not run: {exc}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
            raise TranscriptionError(f"command exited with {proc.returncode}: " + " | ".join(tail))
        return Result(proc.stdout.decode("utf-8", "replace").strip(), language, None)
