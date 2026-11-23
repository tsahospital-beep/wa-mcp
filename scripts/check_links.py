"""Check that every relative link and image in the Markdown files points to something that exists.

    python scripts/check_links.py
    python scripts/check_links.py --pending assets/brand/banner-light.svg --pending assets/brand/banner-dark.svg

Checked: Markdown links and images, and src, srcset and href attributes in raw HTML. Skipped: links with a
scheme (https:, mailto:), fenced code blocks and inline code. A "#fragment" that points into a Markdown file is
checked against the headings of that file.

Files named with --pending are expected to be added later. They are listed separately and do not fail the run.
Exit code 1 when anything else is missing.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

MD_LINK = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+(\S+)", re.MULTILINE)
HTML_ATTR = re.compile(r"""\b(?:src|href|srcset)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
INLINE_CODE = re.compile(r"`+[^`\n]*`+")
FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def markdown_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def strip_code(text: str) -> str:
    """Remove fenced code blocks and inline code so that example links are not checked."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        m = FENCE.match(line)
        if m:
            marker = m.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            out.append(INLINE_CODE.sub("", line))
    return "\n".join(out)


def slug(heading: str) -> str:
    """GitHub style anchor for a heading."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    kept = [c for c in text if c in " -_" or unicodedata.category(c)[0] in "LN"]
    return "".join(kept).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: dict[str, int] = {}
    result: set[str] = set()
    fence: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = FENCE.match(line)
        if m:
            marker = m.group(1)[0]
            fence = marker if fence is None else (None if fence == marker else fence)
            continue
        if fence is not None:
            continue
        h = HEADING.match(line)
        if h:
            base = slug(h.group(2))
            n = found.get(base, 0)
            found[base] = n + 1
            result.add(base if n == 0 else f"{base}-{n}")
    return result


def targets(text: str) -> list[str]:
    body = strip_code(text)
    found = [m.group(1).strip("<>") for m in MD_LINK.finditer(body)]
    found += [m.group(1) for m in REF_DEF.finditer(body)]
    for m in HTML_ATTR.finditer(body):
        value = m.group(1)
        # srcset holds "url descriptor, url descriptor".
        found += [part.split()[0] for part in value.split(",") if part.strip()]
    return found


def check(path: Path, raw: str, pending: set[str]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    later: list[str] = []
    rel_file = path.relative_to(ROOT).as_posix()
    for target in targets(raw):
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("//"):
            continue
        ref, _, fragment = target.partition("#")
        ref = unquote(ref.partition("?")[0])
        base = ROOT if ref.startswith("/") else path.parent
        dest = (base / ref.lstrip("/")).resolve() if ref else path
        try:
            shown = dest.relative_to(ROOT).as_posix()
        except ValueError:
            missing.append(f"{rel_file}: {target} (points outside the repository)")
            continue
        if not dest.exists():
            (later if shown in pending else missing).append(f"{rel_file}: {target}")
            continue
        if fragment and dest.suffix == ".md" and unquote(fragment).lower() not in anchors(dest):
            missing.append(f"{rel_file}: {target} (no heading with this anchor in {shown})")
    return missing, later


def main() -> int:
    parser = argparse.ArgumentParser(description="Check relative links and images in Markdown files")
    parser.add_argument(
        "--pending",
        action="append",
        metavar="PATH",
        help="file that will be added later (repeatable); reported separately",
    )
    args = parser.parse_args()
    pending = set(args.pending or [])
    files = markdown_files()
    missing: list[str] = []
    later: list[str] = []
    checked = 0
    for path in files:
        raw = path.read_text(encoding="utf-8")
        m, p = check(path, raw, pending)
        missing += m
        later += p
        checked += len(targets(raw))
    sys.stdout.write(f"checked {len(files)} Markdown files, {checked} link targets\n")
    if later:
        sys.stdout.write(f"pending (expected to be added later): {len(later)}\n")
        sys.stdout.writelines(f"  {item}\n" for item in later)
    sys.stdout.write(f"missing: {len(missing)}\n")
    sys.stdout.writelines(f"  {item}\n" for item in missing)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
