"""Python library for the wa-mcp WhatsApp bridge."""

__version__ = "0.1.0"


def install_hint(extra: str) -> str:
    """How to add an optional extra from a clone of the repository."""
    return f'uv sync --all-packages --inexact --extra {extra} (repository root) or uv pip install -e "./sdk/python[{extra}]"'
