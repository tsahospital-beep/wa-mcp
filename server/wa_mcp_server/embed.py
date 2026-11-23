"""Add chosen wa-mcp tools to another FastMCP server; see docs/guides/embedding.md."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from wa_mcp import config as config_mod
from wa_mcp_server import app, common

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
            self.seen.add(tool_name)
            if self._target is not None and tool_name in self._include:
                final = self._rename.get(tool_name, tool_name)
                self._target.tool(final, *args, **kwargs)(fn)
                self.added.append(final)
            return fn

        return decorate

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._target or self._scratch, attr)


def add_selected(mcp: FastMCP, wanted: set[str], names: Mapping[str, str]) -> list[str]:
    picker = _Picker(mcp, wanted, names)
    app.register_modules(cast(FastMCP, picker))
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
    unknown = (wanted | set(names)) - tool_names(cfg)
    if unknown:
        raise ValueError(f"unknown wa-mcp tools: {', '.join(sorted(unknown))}")
    return add_selected(mcp, wanted, names)
