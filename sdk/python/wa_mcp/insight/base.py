"""Shared helpers for the insight modules: chat loading, word lists from config, text checks, numbers.

Data comes only from wa_mcp.db, wa_mcp.people and wa_mcp.config. Word lists have English defaults;
config entries extend them (a trailing "*" in a config word means "any ending").
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Any

from wa_mcp import db, people
from wa_mcp.db import Row, TimeArg
from wa_mcp.insight import when
from wa_mcp.insight.wordlists import INSIGHT_WORDS, TASK_WORDS, Words, fold

ME = "me"
THEM = "them"
FILE_KINDS = ("image", "video", "document", "audio")
URL_RE = re.compile(r"(?i)\bhttps?://\S+|\bwww\.\S+")

LAUGH_EMOJI = frozenset({"\U0001f602", "\U0001f923", "\U0001f606", "\U0001f605", "\U0001f639", "\U0001f601"})

# Letters per writing system, as Unicode ranges.
SCRIPTS: tuple[tuple[str, str], ...] = (
    ("latin", "A-Za-z\u00c0-\u024f\u1e00-\u1eff"),
    ("cyrillic", "\u0400-\u052f"),
    ("greek", "\u0370-\u03ff\u1f00-\u1fff"),
    ("georgian", "\u10a0-\u10ff\u1c90-\u1cbf\u2d00-\u2d2f"),
    ("armenian", "\u0530-\u058f"),
    ("hebrew", "\u0590-\u05ff"),
    ("arabic", "\u0600-\u06ff\u0750-\u077f"),
    ("devanagari", "\u0900-\u097f"),
    ("thai", "\u0e00-\u0e7f"),
    ("hangul", "\u1100-\u11ff\uac00-\ud7af"),
    ("kana", "\u3040-\u30ff"),
    ("han", "\u3400-\u4dbf\u4e00-\u9fff"),
)
_SCRIPT_RX = tuple((name, re.compile(f"[{chars}]")) for name, chars in SCRIPTS)


_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
_CLAUSE = re.compile(r"[.,;:!?()\n]")


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    keywords: tuple[str, ...]
    weak: tuple[str, ...] = ()
    chats: tuple[str, ...] = ()
    generic: bool = False


@dataclass(frozen=True)
class Settings:
    task: dict[str, Words]
    words: dict[str, Words]
    projects: tuple[Project, ...]
    watch: tuple[tuple[str, Words], ...]
    burst_gap_minutes: float
    reply_window_minutes: float
    refresh_days: int
    min_task_score: int
    stale_days: int
    keep_days: int
    include_imported: bool
    cache_days: float
    night: tuple[int, int]
    turn_gap_seconds: float
    session_gap_hours: float
    my_names: Words
    negation: frozenset[str]
    date_order: str
    fingerprint: str


def _strings(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError(f"{where}: must be a list of strings")


def _number(node: dict[str, Any], key: str, default: float, lo: float, hi: float, where: str) -> float:
    value = node.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{where}.{key}: must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{where}.{key}: must be between {num(lo)} and {num(hi)}")
    return float(value)


def _flag(node: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = node.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{where}.{key}: must be true or false")
    return value


def _whole(node: dict[str, Any], key: str, default: int, lo: int, hi: int, where: str) -> int:
    value = _number(node, key, default, lo, hi, where)
    if not value.is_integer():
        raise ValueError(f"{where}.{key}: must be a whole number")
    return int(value)


def _section_dict(node: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = node.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where}.{key}: must be an object")
    return value


def _word_map(defaults: dict[str, tuple[str, ...]], extra: dict[str, Any], where: str) -> dict[str, Words]:
    unknown = sorted(set(extra) - set(defaults))
    if unknown:
        raise ValueError(f"{where}: unknown lists {', '.join(unknown)}; known: {', '.join(sorted(defaults))}")
    return {k: Words(v + tuple(_strings(extra.get(k), f"{where}.{k}"))) for k, v in defaults.items()}


def _projects(value: Any) -> tuple[Project, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("tasks.projects: must be a list of {id, name, keywords}")
    out = []
    seen: set[str] = set()
    for i, p in enumerate(value):
        where = f"tasks.projects[{i}]"
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"].strip():
            raise ValueError(f"{where}: needs a string id")
        if p["id"].strip() in seen:
            raise ValueError(f"{where}: duplicate id {p['id'].strip()!r}")
        seen.add(p["id"].strip())
        name = p.get("name") or p["id"]
        if not isinstance(name, str):
            raise ValueError(f"{where}.name: must be a string")
        if not isinstance(p.get("generic", False), bool):
            raise ValueError(f"{where}.generic: must be true or false")
        out.append(
            Project(
                id=p["id"].strip(),
                name=name,
                keywords=tuple(
                    fold(k.strip()) for k in _strings(p.get("keywords"), where + ".keywords") if k.strip()
                ),
                weak=tuple(fold(k.strip()) for k in _strings(p.get("weak"), where + ".weak") if k.strip()),
                chats=tuple(people.bare(c) for c in _strings(p.get("chats"), where + ".chats")),
                generic=bool(p.get("generic", False)),
            )
        )
    return tuple(out)


def _watch(value: Any) -> tuple[tuple[str, Words], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("mentions.watch: must be a list of phrases or {label, words}")
    out = []
    for i, item in enumerate(value):
        if isinstance(item, str):
            if item.strip():
                out.append((item.strip(), Words([item])))
        elif isinstance(item, dict):
            words = _strings(item.get("words"), f"mentions.watch[{i}].words")
            label = item.get("label") or (words[0] if words else "")
            if not isinstance(label, str) or not words:
                raise ValueError(f"mentions.watch[{i}]: need a label and words")
            out.append((label, Words(words)))
        else:
            raise ValueError(f"mentions.watch[{i}]: use a phrase or {{label, words}}")
    return tuple(out)


_settings_cache: list[tuple[Any, Settings]] = []


def _build(cfg: Any) -> Settings:
    tasks, mentions, insight = cfg.section("tasks"), cfg.section("mentions"), cfg.section("insight")
    fp = hashlib.sha1(
        json.dumps([tasks, mentions, insight], sort_keys=True, default=str).encode(), usedforsecurity=False
    ).hexdigest()[:16]
    night = insight.get("night_hours", [0, 7])
    if not (
        isinstance(night, list)
        and len(night) == 2
        and all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in night)
    ):
        raise ValueError("insight.night_hours: need [start_hour, end_hour], hours 0 to 23")
    order = insight.get("date_order", "dmy")
    if order not in ("dmy", "mdy"):
        raise ValueError("insight.date_order: use dmy or mdy")
    words = _word_map(INSIGHT_WORDS, _section_dict(insight, "words", "insight"), "insight.words")
    when.lexicon(cfg)
    return Settings(
        task=_word_map(TASK_WORDS, _section_dict(tasks, "keywords", "tasks"), "tasks.keywords"),
        words=words,
        projects=_projects(tasks.get("projects")),
        watch=_watch(mentions.get("watch")),
        burst_gap_minutes=_number(tasks, "burst_gap_minutes", 4, 0.1, 1440, "tasks"),
        reply_window_minutes=_number(tasks, "reply_window_minutes", 30, 1, 1440, "tasks"),
        refresh_days=_whole(tasks, "refresh_days", 14, 1, 3650, "tasks"),
        min_task_score=_whole(tasks, "min_score", 60, 0, 100, "tasks"),
        stale_days=_whole(tasks, "stale_days", 60, 1, 3650, "tasks"),
        keep_days=_whole(tasks, "keep_days", 365, 1, 36500, "tasks"),
        include_imported=_flag(tasks, "include_imported", False, "tasks"),
        cache_days=_number(insight, "cache_days", 30, 0.01, 3650, "insight"),
        night=(int(night[0]), int(night[1])),
        turn_gap_seconds=_number(insight, "turn_gap_seconds", 180, 1, 86400, "insight"),
        session_gap_hours=_number(insight, "session_gap_hours", 6, 0.1, 720, "insight"),
        my_names=Words(_strings(insight.get("my_names"), "insight.my_names")),
        negation=frozenset(words["negation"].items),
        date_order=str(order),
        fingerprint=fp,
    )


def settings() -> Settings:
    """Insight settings from the current config (sections tasks, mentions, insight), validated once per config."""
    cfg = db.store().cfg
    if _settings_cache and _settings_cache[0][0] is cfg:
        return _settings_cache[0][1]
    s = _build(cfg)
    _settings_cache[:] = [(cfg, s)]
    return s


def affirmed(words: Words, text: str, s: Settings) -> list[str]:
    """Matches of words that are not negated in the same clause ("not done", "isn't sent", "ready? not yet")."""
    low = fold(text)
    out: list[str] = []
    for start, end, item in words.spans(text):
        clause = _CLAUSE.split(low[:start])[-1]
        before = _TOKEN.findall(clause)[-3:]
        after = _TOKEN.findall(low[end:])[:2]
        if any(t in s.negation for t in before) or after[:1] == ["yet"] or after == ["not", "yet"]:
            continue
        if item not in out:
            out.append(item)
    return out


# Done words that only announce, hedge or ask: "almost done", "will be ready tomorrow", "done?".
_NOT_YET_BEFORE = frozenset(
    {
        "almost",
        "nearly",
        "soon",
        "will",
        "gonna",
        "going",
        "should",
        "be",
        "once",
        "when",
        "if",
        "until",
        "till",
    }
)
_NOT_YET_AFTER = frozenset({"by", "tomorrow", "soon", "later", "tonight", "next"})


def said_done(text: str, s: Settings) -> list[str]:
    """Done words ("done", "sent", "ready") that are not negated, hedged, put in the future or asked."""
    low = fold(text)
    definite: set[str] = set()
    for start, end, item in s.task["done"].spans(text):
        before = _TOKEN.findall(_CLAUSE.split(low[:start])[-1])[-3:]
        after = _TOKEN.findall(low[end:])[:1]
        asked = low[end:].lstrip().startswith("?")
        if asked or any(t in _NOT_YET_BEFORE for t in before) or (after and after[0] in _NOT_YET_AFTER):
            continue
        definite.add(item)
    return [w for w in affirmed(s.task["done"], text, s) if w in definite]


# messages


@dataclass
class Msg:
    """One message of a chat, with the sender merged across LID and phone forms."""

    conv: str
    id: str
    who: str
    ts: int
    kind: str
    body: str
    file_name: str = ""
    reply_to: str = ""
    reply_author: str = ""
    reply_preview: str = ""
    mentioned: tuple[str, ...] = ()
    local_path: str = ""
    read_at: int | None = None
    byte_size: int = 0
    transcript: str = ""
    transcript_lang: str | None = None
    transcript_conf: float | None = None
    file_text: str = ""

    @property
    def me(self) -> bool:
        return self.who == ME

    @property
    def side(self) -> str:
        return ME if self.me else THEM

    @cached_property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts)

    @property
    def is_voice(self) -> bool:
        return self.kind == "voice"

    @property
    def text(self) -> str:
        """Typed text, or a reliable transcript for a voice note."""
        if self.body:
            return self.body
        if self.transcript and (self.transcript_conf is None or self.transcript_conf >= 0.6):
            return self.transcript
        return ""

    @cached_property
    def plain(self) -> str:
        return URL_RE.sub("", self.text).strip()

    @property
    def has_link(self) -> bool:
        return bool(URL_RE.search(self.body))

    @property
    def time(self) -> str | None:
        return db.iso_local(self.ts)


@dataclass(frozen=True)
class Target:
    jids: tuple[str, ...]
    name: str
    is_group: bool

    @property
    def key(self) -> str:
        return self.jids[0]


def target(chat: str) -> Target:
    """Resolve a chat given as JID, phone number or name (LID and phone chats merged)."""
    jids = people.resolve_chat(chat)
    return Target(tuple(jids), people.name_of(jids[0]), people.server_of(jids[0]) == people.GROUP_SERVER)


def chat_targets(after: TimeArg = None, before: TimeArg = None) -> list[Target]:
    """Chats with messages in the period, one entry per person or group, most recent first."""
    out: list[Target] = []
    seen: set[str] = set()
    for c in db.active_chats(after=after, before=before):
        jids = people.same_person(str(c["jid"]))
        if jids[0] in seen:
            continue
        seen.add(jids[0])
        out.append(Target(tuple(jids), people.name_of(jids[0]), bool(c["is_group"])))
    return out


def to_msgs(rows: Sequence[Row]) -> list[Msg]:
    derived = db.derived_many([(str(r["conv"]), str(r["msg_id"])) for r in rows])
    mine = set(people.my_jids())
    canon: dict[str, str] = {}
    out = []
    for r in rows:
        author = people.bare(str(r.get("author") or ""))
        if r["outgoing"] or (author and author in mine):
            who = ME
        else:
            key = author or people.bare(str(r["conv"]))
            if key not in canon:
                canon[key] = people.same_person(key)[0]
            who = canon[key]
        m = Msg(
            conv=str(r["conv"]),
            id=str(r["msg_id"]),
            who=who,
            ts=int(r["sent_at"]),
            kind=str(r.get("kind") or "text"),
            body=str(r.get("body") or "").strip(),
            file_name=str(r.get("attachment_name") or ""),
            reply_to=str(r.get("reply_to_id") or ""),
            reply_author=people.bare(str(r.get("reply_to_author") or "")),
            reply_preview=str(r.get("reply_preview") or ""),
            mentioned=tuple(people.bare(str(j)) for j in r.get("mentioned") or []),
            local_path=str(r.get("local_path") or ""),
            read_at=r.get("read_at"),
            byte_size=int(r.get("byte_size") or 0),
        )
        for d in derived.get((m.conv, m.id), []):
            if d["source"] == "transcript":
                m.transcript = str(d["content"]).strip()
                m.transcript_lang = d.get("lang")
                m.transcript_conf = d.get("confidence")
            elif not m.file_text:
                m.file_text = str(d["content"]).strip()
        out.append(m)
    return out


def load(
    convs: Sequence[str], after: TimeArg = None, before: TimeArg = None, imported: bool = True
) -> list[Msg]:
    """All messages of these chats in the period, oldest first. imported=False leaves out messages from
    an imported chat export (ids starting with db.IMPORTED_PREFIX)."""
    rows = db.messages(conv=list(convs), after=after, before=before, ascending=True, limit=None)
    if not imported:
        rows = [r for r in rows if not str(r["msg_id"]).startswith(db.IMPORTED_PREFIX)]
    return to_msgs(rows)


class Names:
    """Display names with a per-call cache; phone-like names are masked."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def __call__(self, who: str) -> str:
        if who == ME:
            return ME
        if who not in self._cache:
            self._cache[who] = mask(people.name_of(who))
        return self._cache[who]


def addressed_to_me(m: Msg, my_msg_ids: set[str], my_jids: set[str], s: Settings) -> list[str]:
    """Why a group message is aimed at me (empty when it is not)."""
    why = []
    if any(j in my_jids for j in m.mentioned):
        why.append("mentions you")
    if (m.reply_to and m.reply_to in my_msg_ids) or (m.reply_author and m.reply_author in my_jids):
        why.append("replies to your message")
    if s.my_names.any(m.text):
        why.append("uses your name")
    return why


# text checks

_PHONE = re.compile(r"(?<![\w+])\+?\d[\d ().-]{6,}\d(?![\d])")
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")
_JID = re.compile(r"\b\d{5,}(?:[.:]\d+)*@(?:lid|s\.whatsapp\.net|g\.us|c\.us)\b")
_MAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?!(?:s\.whatsapp\.net|g\.us|c\.us|lid)\b)([\w-]+\.[\w.-]+)")
_SECRET = re.compile(
    r"(?i)\b(?:password|passwd|passcode|pwd|pin|otp|code|token|secret|api[ _-]?key)\b(?:\s*(?:is|:|=)\s*|\s+)\S*\d"
    r"|\b(?:password|passwd|pwd)\s*[:=]\s*\S+"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{10,}|\bgithub_pat_\w+|\bAKIA[0-9A-Z]{12,}"
    r"|\bhf_[A-Za-z0-9]{10,}|\bxox[abprs]-[A-Za-z0-9-]{8,}|\beyJ[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|\bAIza[0-9A-Za-z_-]{20,}|^\s*\d{4,8}\s*$|\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,}"
)


def looks_secret(text: str) -> bool:
    """Tokens, passwords, one-time codes, bank numbers: such text never goes into examples."""
    return bool(text) and bool(_SECRET.search(text))


def _phone_sub(m: re.Match[str]) -> str:
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8 or len(digits) > 15 or _DATEISH.match(raw.strip()):
        return raw
    return "***" + digits[-2:]


def _url_sub(m: re.Match[str]) -> str:
    url = m.group(0)
    host = re.sub(r"(?i)^https?://", "", url).split("/", 1)[0].split("?", 1)[0]
    return host + ("/..." if len(url) > len(host) + 9 else "")


def mask(text: str) -> str:
    """Hide phone numbers, JIDs and mail users; shorten links to their host."""
    if not text:
        return ""
    text = _JID.sub(lambda m: "***" + m.group(0).split("@")[0][-2:] + "@" + m.group(0).split("@")[1], text)
    text = URL_RE.sub(_url_sub, text)
    text = _MAIL.sub(lambda m: "***@" + m.group(1), text)
    return _PHONE.sub(_phone_sub, text)


def clip(text: str, n: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def safe_text(text: str, n: int = 80) -> str | None:
    """Masked, clipped text for an example, or None when it looks like a secret.

    The text is cut to a few times n before masking (masking cost grows with length); the final clip
    to n drops the cut edge, so a number or address split by the first cut is never shown.
    """
    if not text or looks_secret(text):
        return None
    return clip(mask(text[: max(4 * n, 400)]), n)


def example(m: Msg, who: str, text: str | None = None, n: int = 80, **extra: Any) -> dict[str, Any] | None:
    shown = safe_text(m.text if text is None else text, n)
    if shown is None:
        return None
    out: dict[str, Any] = {"time": m.dt.strftime("%Y-%m-%d %H:%M"), "who": who, "text": shown, "id": m.id}
    if text is None and not m.body and m.transcript:
        out["source"] = "voice"
    out.update(extra)
    return out


def tokens(text: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", URL_RE.sub(" ", text or "").casefold())
        if len(w) <= 24
    ]


def _is_emoji(c: int) -> bool:
    return (
        0x1F000 <= c <= 0x1FAFF
        or 0x2600 <= c <= 0x27BF
        or 0x2B00 <= c <= 0x2BFF
        or 0x23E9 <= c <= 0x23FA
        or 0x25AA <= c <= 0x25FE
        or c in (0x231A, 0x231B, 0x2328, 0x23CF, 0x24C2, 0x2934, 0x2935, 0x3030, 0x303D, 0x3297, 0x3299)
        or c in (0x203C, 0x2049, 0x2139)
    )


def _is_modifier(c: int) -> bool:
    return c in (0xFE0F, 0x20E3) or 0x1F3FB <= c <= 0x1F3FF or 0xE0020 <= c <= 0xE007F


def emoji_clusters(text: str) -> list[str]:
    """Emoji as graphemes: ZWJ sequences, skin tones, flags (regional indicator pairs) and keycaps stay whole."""
    out: list[str] = []
    i, n = 0, len(text or "")
    while i < n:
        c = ord(text[i])
        if 0x1F1E6 <= c <= 0x1F1FF:
            pair = i + 1 < n and 0x1F1E6 <= ord(text[i + 1]) <= 0x1F1FF
            out.append(text[i : i + (2 if pair else 1)])
            i += 2 if pair else 1
            continue
        if text[i] in "0123456789#*" and i + 1 < n and ord(text[i + 1]) in (0xFE0F, 0x20E3):
            j = i + 1
            while j < n and ord(text[j]) in (0xFE0F, 0x20E3):
                j += 1
            if 0x20E3 in (ord(ch) for ch in text[i + 1 : j]):
                out.append(text[i:j])
                i = j
                continue
        if _is_emoji(c) or (c in (0x00A9, 0x00AE, 0x2122) and i + 1 < n and ord(text[i + 1]) == 0xFE0F):
            j = i + 1
            while j < n:
                cj = ord(text[j])
                if _is_modifier(cj):
                    j += 1
                elif cj == 0x200D and j + 1 < n and _is_emoji(ord(text[j + 1])):
                    j += 2
                else:
                    break
            out.append(text[i:j])
            i = j
            continue
        i += 1
    return out


def script_counts(text: str) -> Counter[str]:
    """Letters per writing system; links are ignored."""
    text = URL_RE.sub("", text or "")
    counts: Counter[str] = Counter()
    for name, rx in _SCRIPT_RX:
        k = len(rx.findall(text))
        if k:
            counts[name] = k
    return counts


def script_of(text: str) -> str:
    counts = script_counts(text)
    return counts.most_common(1)[0][0] if counts else "none"


def is_laugh(text: str, s: Settings) -> bool:
    return s.words["laugh"].any(text) or any(e in LAUGH_EMOJI for e in emoji_clusters(text))


# numbers


def pct(values: Iterable[float | None], p: float, nd: int = 1) -> float | None:
    xs = sorted(x for x in values if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    v = xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
    return round(v, nd)


def median(values: Iterable[float | None], nd: int = 1) -> float | None:
    return pct(values, 50, nd)


def share(a: float, b: float, nd: int = 2) -> float | None:
    return round(a / b, nd) if b else None


def per100(a: float, b: float) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def fmt_minutes(m: float | None) -> str:
    if m is None:
        return "n/a"
    if m < 1:
        return f"{round(m * 60)} s"
    if m < 90:
        return f"{round(m)} min"
    if m < 36 * 60:
        return f"{round(m / 60, 1)} h"
    return f"{round(m / 1440, 1)} d"


def num(x: float | None) -> str:
    if x is None:
        return "n/a"
    return str(int(x)) if float(x).is_integer() else str(round(x, 1))
