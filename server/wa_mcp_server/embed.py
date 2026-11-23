"""Add chosen wa-mcp tools to another FastMCP server; see docs/guides/embedding.md."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from wa_mcp import config as config_mod
from wa_mcp_server import app, common, toolsets

Tool = Callable[..., Any]


class _Picker:
    """Stands in for FastMCP while the tool modules register: adds only the chosen tools."""

    def __init__(self, target: FastMCP | None, include: set[str], rename: Mapping[str, str]) -> None:
        self._target = target
        self._scratch = FastMCP("wa-mcp-names")
        self._include = include
        self._rename = rename
        self.seen: set[str] = set()
        self.added: list[str] = []

    def tool(self, name: str | None = None, *args: Any, **kwargs: Any) -> Callable[[Tool], Tool]:
        def decorate(fn: Tool) -> Tool:
            tool_name = name or fn.__name__
            # task_detail has its own handler for its historical query argument.
            aliases = [
                old for old, new in toolsets.LEGACY_NAMES.items() if new == tool_name and old != "task_detail"
            ]
            for public in [tool_name, *aliases]:
                if public not in toolsets.LEGACY_NAMES:
                    self.seen.add(public)
                if self._target is None or public not in self._include:
                    continue
                final = self._rename.get(public, public)
                options = dict(kwargs)
                if public in toolsets.LEGACY_NAMES:
                    options["description"] = (
                        f"Deprecated alias for {toolsets.LEGACY_NAMES[public]}. {fn.__doc__ or ''}"
                    )
                options.update(title=kwargs["annotations"].title, structured_output=True)
                self._target.tool(final, *args, **options)(fn)
                self.added.append(final)
            return fn

        return decorate

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._target or self._scratch, attr)


def add_selected(mcp: FastMCP, wanted: set[str], names: Mapping[str, str]) -> list[str]:
    picker = _Picker(mcp, wanted, names)
    app.register_modules(cast(FastMCP, picker), legacy_detail="task_detail" in wanted)
    return picker.added


def tool_names(cfg: config_mod.Config | None = None) -> set[str]:
    """Names of all wa-mcp tools that register() can add."""
    common.setup(cfg or config_mod.load())
    picker = _Picker(None, set(), {})
    app.register_modules(cast(FastMCP, picker))
    return picker.seen


def register(
    mcp: FastMCP,
    include: Iterable[str],
    cfg: config_mod.Config | None = None,
    rename: Mapping[str, str] | None = None,
) -> list[str]:
    """Add the wa-mcp tools named in include to mcp and return the names they got.

    cfg is the wa-mcp config (default: wa_mcp.config.load()). rename gives a tool another
    name, for tools whose name mcp already uses. Names that no wa-mcp tool has raise
    ValueError before anything is added. The tools share one config per process.
    """
    wanted = set(include)
    names = dict(rename or {})
    unknown = (wanted | set(names)) - (tool_names(cfg) | toolsets.LEGACY_NAMES.keys())
    if unknown:
        raise ValueError(f"unknown wa-mcp tools: {', '.join(sorted(unknown))}")
    return add_selected(mcp, wanted, names)
