"""Add chosen wa-mcp tools to another FastMCP server; see docs/guides/embedding.md."""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from wa_mcp import config as config_mod
from wa_mcp_server import app, common

log = logging.getLogger("wa_mcp_server")

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


def _register_all(picker: _Picker) -> None:
    mcp = cast(FastMCP, picker)
    for module in app.MODULES:
        module.register(mcp)
    for name in app.OPTIONAL_MODULES:
        if importlib.util.find_spec(name) is None:
            continue
        try:
            importlib.import_module(name).register(mcp)
        except Exception as exc:
            log.exception("optional tool module %s failed to load", name)
            common.load_errors[name] = f"{exc.__class__.__name__}: {exc}"


def tool_names(cfg: config_mod.Config | None = None) -> set[str]:
    """Names of all wa-mcp tools that register() can add."""
    common.setup(cfg or config_mod.load())
    picker = _Picker(None, set(), {})
    _register_all(picker)
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
    picker = _Picker(mcp, wanted, names)
    _register_all(picker)
    return picker.added
