"""Include the repository's default prompt in SDK wheels and source archives."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        target = "wa_mcp/prompts/agent-default.md"
        if not (root / target).is_file():
            build_data["force_include"][str(root.parent.parent / "prompts" / "agent-default.md")] = target
