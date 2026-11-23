"""Local text extraction from received files. Nothing is ever executed.

Office files are read as ZIP + XML with the standard library. PDF needs pypdf,
RAR listing needs rarfile (optional extra "files"). Archives
are listed, never unpacked to disk; small text members are read in memory.
Limits guard against oversized inputs and ZIP bombs.
"""

from __future__ import annotations

import html.parser
import struct
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET

from wa_mcp import db, install_hint

FILES_HINT = install_hint("files")


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_text_chars: int = 200_000
    max_members: int = 2_000
    max_total_uncompressed: int = 200 * 1024 * 1024
    max_ratio: float = 100.0
    max_member_text_bytes: int = 1024 * 1024
    max_xml_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 500


DEFAULT_LIMITS = Limits()

EXECUTABLE_SUFFIXES = frozenset(
    [
        ".exe",
        ".dll",
        ".com",
        ".scr",
        ".msi",
        ".msp",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".jar",
        ".apk",
        ".app",
        ".sh",
        ".bash",
        ".zsh",
        ".bin",
        ".run",
        ".elf",
        ".so",
        ".dylib",
        ".lnk",
        ".reg",
        ".cpl",
        ".sys",
        ".drv",
        ".ocx",
        ".pif",
        ".appx",
        ".msix",
        ".deb",
        ".rpm",
    ]
)
TEXT_SUFFIXES = frozenset(
    [".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml", ".ini", ".srt", ".vtt"]
)
HTML_SUFFIXES = frozenset({".html", ".htm"})


class FileError(Exception):
    """The file cannot be read safely."""


@dataclass
class Extracted:
    text: str
    source: str  # document | archive
    kind: str
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "source": self.source, "kind": self.kind, "truncated": self.truncated}


def _cap(text: str, limits: Limits) -> tuple[str, bool]:
    if len(text) > limits.max_text_chars:
        return text[: limits.max_text_chars], True
    return text, False


_EOCD = b"PK\x05\x06"
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_EOCD = b"PK\x06\x06"


def _directory_span(fh: BinaryIO) -> tuple[int, int, int]:
    """Locate the central directory, including archives with a leading stub."""
    size = fh.seek(0, 2)
    start = max(0, size - 65535 - 22)
    fh.seek(start)
    tail = fh.read()
    at = tail.rfind(_EOCD)
    if at < 0 or len(tail) - at < 22:
        raise FileError("not a valid ZIP file: missing end record")
    end = start + at
    entries, length = struct.unpack_from("<HI", tail, at + 10)
    if end >= 20:
        fh.seek(end - 20)
        if fh.read(4) == _ZIP64_LOCATOR:
            end -= 20 + 56
            if end < 0:
                raise FileError("not a valid ZIP64 file")
            fh.seek(end)
            record = fh.read(56)
            if record[:4] != _ZIP64_EOCD:
                raise FileError("not a valid ZIP64 end record")
            entries, length = struct.unpack_from("<QQ", record, 32)
    if length > end:
        raise FileError("not a valid ZIP central directory")
    return end - length, end, entries


def _check_member_count(fh: BinaryIO, limit: int) -> None:
    start, end, reported = _directory_span(fh)
    if reported > limit:
        raise FileError(f"archive has {reported} entries (limit {limit})")
    fh.seek(start)
    count = 0
    while fh.tell() < end:
        header = fh.read(46)
        if len(header) != 46 or not header.startswith(b"PK\x01\x02"):
            raise FileError("not a valid ZIP directory entry")
        count += 1
        if count > limit:
            raise FileError(f"archive has more than {limit} entries")
        fh.seek(sum(struct.unpack_from("<HHH", header, 28)), 1)
    if fh.tell() != end or count != reported:
        raise FileError("inconsistent ZIP central directory")


@contextmanager
def _open_zip(path: Path, limits: Limits, what: str) -> Iterator[zipfile.ZipFile]:
    with path.open("rb") as fh:
        _check_member_count(fh, limits.max_members)
        try:
            with zipfile.ZipFile(fh) as archive:
                yield archive
        except zipfile.BadZipFile as exc:
            raise FileError(f"not a valid {what}: {exc}") from exc


def _check_zip(zf: zipfile.ZipFile, limits: Limits) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise FileError(f"archive has {len(infos)} entries (limit {limits.max_members})")
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos)
    if total > limits.max_total_uncompressed:
        raise FileError(f"archive unpacks to {total} bytes (limit {limits.max_total_uncompressed})")
    if packed and total / packed > limits.max_ratio:
        raise FileError(f"archive compression ratio {total / packed:.0f} is above {limits.max_ratio:.0f}")
    for i in infos:
        if i.compress_size and i.file_size / i.compress_size > limits.max_ratio * 10:
            raise FileError(f"entry {i.filename!r} has a suspicious compression ratio")
    return infos


class _Zip:
    """Reads members with a per-member cap and a running total of bytes unpacked."""

    def __init__(self, zf: zipfile.ZipFile, limits: Limits) -> None:
        self.zf = zf
        self.limits = limits
        self.spent = 0

    def read(self, info: zipfile.ZipInfo, cap: int) -> bytes:
        """Read at most cap bytes, whatever the header claims."""
        with self.zf.open(info) as fh:
            data = fh.read(cap + 1)
        if len(data) > cap:
            raise FileError(f"entry {info.filename!r} is larger than {cap} bytes")
        self.spent += len(data)
        if self.spent > self.limits.max_total_uncompressed:
            raise FileError(f"archive unpacks to more than {self.limits.max_total_uncompressed} bytes")
        return data

    def xml(self, name: str) -> ET.Element:
        return _xml(self.read(self.zf.getinfo(name), self.limits.max_xml_bytes))

    def names(self) -> list[str]:
        return self.zf.namelist()


def _has_dtd(data: bytes) -> bool:
    """DOCTYPE or ENTITY in any encoding the XML parser accepts (UTF-8 and its ASCII subsets, UTF-16)."""
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        text = data.decode(encoding, errors="ignore")
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            return True
    return False


def _xml(data: bytes) -> ET.Element:
    if _has_dtd(data):
        raise FileError("XML with DTD or entities is refused")
    try:
        return ET.fromstring(data)  # noqa: S314
    except ET.ParseError as exc:
        raise FileError(f"bad XML: {exc}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paragraphs(root: ET.Element, para: str, text: str, tab: str | None = None) -> list[str]:
    out: list[str] = []
    for p in root.iter():
        if _local(p.tag) != para:
            continue
        parts: list[str] = []
        for node in p.iter():
            name = _local(node.tag)
            if name == text and node.text:
                parts.append(node.text)
            elif tab and name == tab:
                parts.append("\t")
        line = "".join(parts).strip()
        if line:
            out.append(line)
    return out


def _docx(zr: _Zip) -> str:
    names = set(zr.names())
    blocks: list[str] = []
    for name in ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]:
        if name in names:
            root = zr.xml(name)
            blocks.extend(_paragraphs(root, "p", "t", tab="tab"))
    return "\n".join(blocks)


def _numbered(names: list[str], prefix: str) -> list[str]:
    numbered = []
    for name in names:
        digits = name.removeprefix(prefix).removesuffix(".xml")
        if name == prefix + digits + ".xml" and digits.isascii() and digits.isdecimal():
            number = digits.lstrip("0")
            numbered.append((len(number), number, name))
    return [name for _, _, name in sorted(numbered)]


def _pptx(zr: _Zip) -> str:
    out: list[str] = []
    for i, name in enumerate(_numbered(zr.names(), "ppt/slides/slide"), start=1):
        root = zr.xml(name)
        lines = _paragraphs(root, "p", "t")
        out.append(f"--- slide {i} ---")
        out.extend(lines)
    return "\n".join(out)


def _xlsx(zr: _Zip) -> str:
    names = zr.names()
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = zr.xml("xl/sharedStrings.xml")
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    titles: list[str] = []
    if "xl/workbook.xml" in names:
        wb = zr.xml("xl/workbook.xml")
        titles = [s.get("name", "") for s in wb.iter() if _local(s.tag) == "sheet"]
    out: list[str] = []
    remaining = zr.limits.max_text_chars + 1

    def append(text: str) -> bool:
        nonlocal remaining
        piece = text[:remaining]
        out.append(piece)
        remaining -= len(piece)
        return remaining > 0

    for i, name in enumerate(_numbered(names, "xl/worksheets/sheet")):
        root = zr.xml(name)
        title = f"--- sheet {titles[i] if i < len(titles) else i + 1} ---"
        if not append(("\n" if out else "") + title):
            break
        for row in root.iter():
            if _local(row.tag) != "row":
                continue
            empty_cells = 0
            started = False
            for c in row:
                if _local(c.tag) != "c":
                    continue
                kind = c.get("t", "")
                value = ""
                for node in c.iter():
                    tag = _local(node.tag)
                    if tag == "v" and node.text is not None:
                        value = node.text
                    elif tag == "t" and kind == "inlineStr" and node.text:
                        value += node.text
                if kind == "s":
                    index = value.lstrip("0") or "0"
                    if value.isascii() and value.isdecimal() and len(index) <= len(str(len(shared))):
                        pos = int(index)
                        if pos < len(shared):
                            value = shared[pos]
                if not started and not value:
                    empty_cells += 1
                    continue
                if started:
                    sep = "\t"
                else:
                    sep = "\n" + "\t" * min(empty_cells, remaining)
                    started = True
                if not append(sep) or not append(value):
                    return "".join(out)
    return "".join(out)


class _HTMLText(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        return text
    return data.decode("latin-1")


def _pdf(path: Path, limits: Limits) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise FileError(f"PDF support is not installed: {FILES_HINT}") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise FileError("the PDF is password protected")
        out: list[str] = []
        size = 0
        for i, page in enumerate(reader.pages):
            if i >= limits.max_pdf_pages or size > limits.max_text_chars:
                break
            text = page.extract_text() or ""
            size += len(text)
            out.append(text.strip())
    except FileError:
        raise
    except Exception as exc:
        raise FileError(f"cannot read PDF: {exc}") from exc
    return "\n\n".join(t for t in out if t)


def _zip_inventory(path: Path, limits: Limits) -> str:
    with _open_zip(path, limits, "ZIP file") as zf:
        infos = _check_zip(zf, limits)
        zr = _Zip(zf, limits)
        lines = [f"ZIP archive, {len(infos)} entries, {sum(i.file_size for i in infos)} bytes unpacked"]
        texts: list[str] = []
        for info in infos:
            name = info.filename
            suffix = Path(name).suffix.lower()
            flag = ""
            if suffix in EXECUTABLE_SUFFIXES:
                flag = "  [executable, not opened]"
            elif info.flag_bits & 0x1:
                flag = "  [encrypted]"
            lines.append(f"- {name} ({info.file_size} bytes){flag}")
            readable = suffix in TEXT_SUFFIXES or suffix in HTML_SUFFIXES
            if readable and not flag and not info.is_dir() and info.file_size <= limits.max_member_text_bytes:
                data = zr.read(info, limits.max_member_text_bytes)
                text = _decode(data)
                if suffix in HTML_SUFFIXES:
                    text = html_text(text)
                texts.append(f"--- {name} ---\n{text.strip()}")
        return "\n".join(lines + texts)


def _rar_inventory(path: Path, limits: Limits) -> str:
    try:
        import rarfile
    except ImportError as exc:
        raise FileError(f"RAR listing is not installed: {FILES_HINT}") from exc
    try:
        with rarfile.RarFile(str(path)) as rf:
            infos = rf.infolist()
    except Exception as exc:
        raise FileError(f"cannot list RAR archive: {exc}") from exc
    if len(infos) > limits.max_members:
        raise FileError(f"archive has {len(infos)} entries (limit {limits.max_members})")
    lines = [f"RAR archive, {len(infos)} entries (listed only)"]
    for info in infos:
        name = str(info.filename)
        flag = "  [executable]" if Path(name).suffix.lower() in EXECUTABLE_SUFFIXES else ""
        lines.append(f"- {name} ({int(info.file_size)} bytes){flag}")
    return "\n".join(lines)


def extract_text(path: str | Path, limits: Limits = DEFAULT_LIMITS) -> Extracted:
    """Text of a document, or the inventory of an archive."""
    p = Path(path)
    if not p.is_file():
        raise FileError(f"no such file: {p}")
    size = p.stat().st_size
    if size > limits.max_file_bytes:
        raise FileError(f"file is {size} bytes (limit {limits.max_file_bytes})")
    suffix = p.suffix.lower()
    with p.open("rb") as fh:
        head = fh.read(8)
    if suffix in EXECUTABLE_SUFFIXES:
        raise FileError("executable files are not opened")
    if suffix == ".pdf" or head.startswith(b"%PDF"):
        text, kind, source = _pdf(p, limits), "pdf", "document"
    elif suffix in (".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"):
        with _open_zip(p, limits, "Office file") as zf:
            _check_zip(zf, limits)
            reader = {"d": _docx, "x": _xlsx, "p": _pptx}[suffix[1]]
            text, kind, source = reader(_Zip(zf, limits)), suffix[1:5], "document"
    elif suffix == ".zip" or head.startswith(b"PK\x03\x04"):
        text, kind, source = _zip_inventory(p, limits), "zip", "archive"
    elif suffix == ".rar" or head.startswith(b"Rar!"):
        text, kind, source = _rar_inventory(p, limits), "rar", "archive"
    elif suffix in HTML_SUFFIXES:
        text, kind, source = html_text(_decode(p.read_bytes())), "html", "document"
    elif suffix in TEXT_SUFFIXES:
        text, kind, source = _decode(p.read_bytes()), suffix[1:], "document"
    else:
        raise FileError(f"unsupported file type {suffix or '(none)'}")
    capped, truncated = _cap(text.strip(), limits)
    return Extracted(capped, source, kind, truncated)


def extract_message(conv: str, msg_id: str, path: str | Path, limits: Limits = DEFAULT_LIMITS) -> Extracted:
    """Extract and store the result in derived_text (source document or archive)."""
    result = extract_text(path, limits)
    db.put_derived(conv, msg_id, result.source, result.text)
    return result
