"""Run any local program: argv template with {file} and {language}; text from stdout. No shell."""

from __future__ import annotations

import os
import shutil
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

        if "{file}" in self.argv[0] or "{language}" in self.argv[0]:
            raise BackendUnavailable("transcription.command.argv needs a fixed program name")
        if os.name == "nt" and Path(shutil.which(self.argv[0]) or self.argv[0]).suffix.lower() in (
            ".bat",
            ".cmd",
        ):
            raise BackendUnavailable("use a native executable for transcription.command.argv on Windows")

    def transcribe(self, wav: Path, language: str | None = None) -> Result:
        self.check()
        argv = [a.replace("{file}", str(wav)).replace("{language}", language or "auto") for a in self.argv]
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TranscriptionError("transcription command timed out") from None
        except OSError as exc:
            raise TranscriptionError(f"command did not run: {exc.__class__.__name__}") from None
        if proc.returncode != 0:
            raise TranscriptionError(f"command exited with {proc.returncode}")
        return Result(proc.stdout.decode("utf-8", "replace").strip(), language, None)
