"""Due dates in message text ("tomorrow", "on Friday", "by 5 pm", "2026-03-05", "March 5"), relative to a clock.

English patterns; more names can be added in config insight.dates (keys: today, tonight, tomorrow,
day_after_tomorrow, next_week, this_week, weekend, end_of_day, weekdays (7 lists), months (12 lists)).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from wa_mcp import db

_DEFAULT: dict[str, list[str]] = {
    "today": ["today", "this afternoon", "this morning"],
    "end_of_day": ["end of day", "end of the day", "eod", "by close of business", "cob"],
    "tonight": ["tonight", "this evening"],
    "tomorrow": ["tomorrow", "tmrw", "tmr"],
    "day_after_tomorrow": ["day after tomorrow", "the day after tomorrow"],
    "next_week": ["next week"],
    "this_week": ["this week", "end of the week", "end of week"],
    "weekend": ["weekend", "this weekend"],
}
# Full name first; shorter forms only count after a word like "on" or "by".
_WEEKDAYS = [
    ["monday", "mon"], ["tuesday", "tue", "tues"], ["wednesday", "wed"], ["thursday", "thu", "thur", "thurs"],
    ["friday", "fri"], ["saturday", "sat"], ["sunday", "sun"],
]  # fmt: skip
_MONTHS = [
    ["january", "jan"], ["february", "feb"], ["march", "mar"], ["april", "apr"], ["may"], ["june", "jun"],
    ["july", "jul"], ["august", "aug"], ["september", "sep", "sept"], ["october", "oct"], ["november", "nov"],
    ["december", "dec"],
]  # fmt: skip

_MORNING, _EVENING, _END_OF_DAY = time(10, 0), time(20, 0), time(18, 0)
# Dates further than this from the message (either way) are ignored; also keeps timestamps in range on Windows.
MAX_AHEAD_DAYS = 3650


@dataclass(frozen=True)
class Due:
    """A due moment found in text. all_day means only the day is known (due at the end of that day)."""

    at: datetime
    all_day: bool
    phrase: str

    @property
    def day(self) -> date:
        return self.at.date()

    def end(self) -> datetime:
        return datetime.combine(self.day, time(23, 59, 59)) if self.all_day else self.at

    def hint(self) -> str:
        stamp = self.at.strftime("%Y-%m-%d") if self.all_day else self.at.strftime("%Y-%m-%d %H:%M")
        return f"{self.phrase} ({stamp})"


@dataclass(frozen=True)
class Lexicon:
    words: dict[str, list[str]]
    weekdays: list[list[str]]
    months: list[list[str]]
    order: str


def _alt(items: list[str]) -> str:
    words = sorted(set(items), key=len, reverse=True)
    return "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in words)


_lex_cache: list[tuple[Any, Lexicon]] = []


def _names(value: Any, where: str) -> list[str]:
    items = value if isinstance(value, list) else [value]
    if not all(isinstance(n, str) and n.strip() for n in items):
        raise ValueError(f"{where}: must be names (strings)")
    return [n.strip().casefold() for n in items]


def lexicon(cfg: Any = None) -> Lexicon:
    """Day and month names from the defaults plus config insight.dates, validated and cached per config."""
    cfg = cfg if cfg is not None else db.store().cfg
    if _lex_cache and _lex_cache[0][0] is cfg:
        return _lex_cache[0][1]
    section = cfg.section("insight")
    extra: Any = section.get("dates") or {}
    if not isinstance(extra, dict):
        raise ValueError("insight.dates: must be an object")
    unknown = sorted(set(extra) - set(_DEFAULT) - {"weekdays", "months"})
    if unknown:
        raise ValueError(f"insight.dates: unknown keys {', '.join(unknown)}")
    words = {k: v + _names(extra.get(k, []), f"insight.dates.{k}") for k, v in _DEFAULT.items()}
    weekdays = [list(d) for d in _WEEKDAYS]
    months = [list(m) for m in _MONTHS]
    for key, table, size in (("weekdays", weekdays, 7), ("months", months, 12)):
        more = extra.get(key)
        if more is None:
            continue
        if not isinstance(more, list) or len(more) != size:
            raise ValueError(f"insight.dates.{key}: need {size} lists of names")
        for i, names in enumerate(more):
            table[i] += _names(names, f"insight.dates.{key}[{i}]")
    lex = Lexicon(words, weekdays, months, str(section.get("date_order", "dmy")))
    _lex_cache[:] = [(cfg, lex)]
    return lex


def next_midnight(d: date) -> datetime:
    """Local midnight that starts the day after d (correct on days with a clock change)."""
    return datetime.combine(d + timedelta(days=1), time.min)


def day_bounds(d: date) -> tuple[int, int]:
    """Unix seconds of local midnight at the start of d and of the next day."""
    try:
        return int(datetime.combine(d, time.min).timestamp()), int(next_midnight(d).timestamp())
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError(f"date {d} is out of range") from exc


def _clock(hour: int, minute: int, ampm: str) -> time | None:
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


_TIME = re.compile(
    r"(?<!\w)(?:by|at|before|until|till|around)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?!\w)"
    r"|(?<![\w:])(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?!\w)"
    r"|(?<![\w:])(\d{1,2}):(\d{2})(?![\w:])"
)
_REL = re.compile(r"(?<!\w)in\s+(\d+|an?|a\s+couple\s+of|a\s+few)\s+(minute|min|hour|hr|day|week)s?(?!\w)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NUMERIC = re.compile(r"(?<![\d/.])(\d{1,2})(?:/(\d{1,2})(?:/(\d{2,4}))?|\.(\d{1,2})\.(\d{2,4}))(?![\d/.])")
_PREFIX = r"(?:(next|this|on|by|until|till)\s+)"


def _find_time(text: str) -> tuple[time, str] | None:
    for m in _TIME.finditer(text):
        g = m.groups()
        if g[0] is not None:
            hour, minute, ampm = int(g[0]), int(g[1] or 0), (g[2] or "").replace(".", "")
            if not ampm and g[1] is None:
                if not 1 <= hour <= 12:
                    continue
                if hour < 8:
                    ampm = "pm"
        elif g[3] is not None:
            hour, minute, ampm = int(g[3]), int(g[4] or 0), g[5].replace(".", "")
        else:
            hour, minute, ampm = int(g[6]), int(g[7]), ""
        t = _clock(hour, minute, ampm)
        if t is not None:
            return t, m.group(0).strip()
    return None


def _next_weekday(ref: date, wd: int, mode: str) -> date:
    ahead = (wd - ref.weekday()) % 7
    if ahead == 0 and mode != "this":
        ahead = 7
    day = ref + timedelta(days=ahead)
    if mode == "next" and wd > ref.weekday():
        day += timedelta(days=7)
    return day


def _roll(month: int, day: int, today: date) -> date | None:
    """A day without a year: this year, or the next year that has it when it is long past.

    Feb 29 outside a leap year means Feb 29 of the next leap year (never Mar 1). None for a day
    that no year has (Feb 30, Apr 31).
    """
    for year in range(today.year, today.year + 9):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= today - timedelta(days=60):
            return d
    return None


def _within(d: date, today: date) -> bool:
    return abs((d - today).days) <= MAX_AHEAD_DAYS


def _find_day(text: str, ref: datetime, lex: Lexicon) -> tuple[date, str, time | None] | None:
    """(day, phrase, default time) for the first day expression in text."""
    w = lex.words
    today = ref.date()
    hits: list[tuple[int, date, str, time | None]] = []

    def add(rx: str, day: date, default: time | None) -> None:
        for m in re.finditer(r"(?<!\w)(?:" + rx + r")(?!\w)", text):
            hits.append((m.start(), day, m.group(0), default))

    tomorrow = today + timedelta(days=1)
    add(_alt(w["day_after_tomorrow"]), today + timedelta(days=2), None)
    add(r"(?:" + _alt(w["tomorrow"]) + r")\s+morning", tomorrow, _MORNING)
    add(r"(?:" + _alt(w["tomorrow"]) + r")\s+(?:evening|night)", tomorrow, _EVENING)
    add(_alt(w["tomorrow"]), tomorrow, None)
    add(_alt(w["tonight"]), today, _EVENING)
    add(_alt(w["end_of_day"]), today, _END_OF_DAY)
    add(_alt(w["today"]), today, None)
    add(_alt(w["next_week"]), today + timedelta(days=7 - today.weekday()), None)
    add(_alt(w["this_week"]), today + timedelta(days=max(0, 4 - today.weekday())), None)
    add(_alt(w["weekend"]), today + timedelta(days=(5 - today.weekday()) % 7), None)
    for i, names in enumerate(lex.weekdays):
        full, short = names[:1] + [n for n in names[1:] if len(n) > 4], [n for n in names[1:] if len(n) <= 4]
        pats = [_PREFIX + "?(?:" + _alt(full) + ")"] + (
            [_PREFIX + "(?:" + _alt(short) + r")\.?"] if short else []
        )
        for pat in pats:
            for m in re.finditer(r"(?<!\w)" + pat + r"(?!\w)", text):
                hits.append((m.start(), _next_weekday(today, i, m.group(1) or ""), m.group(0), None))
    for m in _ISO.finditer(text):
        try:
            iso = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if _within(iso, today):
            hits.append((m.start(), iso, m.group(0), None))
    month_of = {name: i + 1 for i, names in enumerate(lex.months) for name in names}
    names_rx = _alt(list(month_of))
    named = re.compile(
        r"(?<!\w)(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + names_rx + r")"
        r"|(" + names_rx + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?)(?!\w)"
    )
    for m in named.finditer(text):
        day_s, month_s = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        month = month_of.get(re.sub(r"\s+", " ", month_s), 0)
        rolled = _roll(month, int(day_s), today) if month else None
        if rolled is not None:
            hits.append((m.start(), rolled, m.group(0), None))
    for m in _NUMERIC.finditer(text):
        first = int(m.group(1))
        second, year_s = (m.group(2), m.group(3)) if m.group(2) else (m.group(4), m.group(5))
        dd, mm = (first, int(second)) if lex.order == "dmy" else (int(second), first)
        if year_s is None:
            found = _roll(mm, dd, today)
        else:
            try:
                found = date(int(year_s) + 2000 if len(year_s) == 2 else int(year_s), mm, dd)
            except ValueError:
                continue
        if found is not None and _within(found, today):
            hits.append((m.start(), found, m.group(0), None))
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], -len(h[2])))
    _, day, phrase, default = hits[0]
    return day, phrase, default


def parse_due(text: str, ref: datetime, lex: Lexicon | None = None) -> Due | None:
    """First due moment in text relative to ref (the message time). None when the text names no time."""
    if not text:
        return None
    try:
        return _parse(text, ref, lex or lexicon())
    except (OverflowError, ValueError):
        return None


def _parse(text: str, ref: datetime, lex: Lexicon) -> Due | None:
    low = text.casefold()
    rel = _REL.search(low)
    if rel:
        qty_s, unit = rel.group(1), rel.group(2)
        qty = int(qty_s) if qty_s.isdigit() else (2 if "couple" in qty_s else 3 if "few" in qty_s else 1)
        minutes = {"minute": 1, "min": 1, "hour": 60, "hr": 60, "day": 1440, "week": 10080}[unit] * qty
        if minutes > MAX_AHEAD_DAYS * 1440:
            return None
        at = (ref + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
        return Due(at, unit in ("day", "week"), rel.group(0))
    found_day = _find_day(low, ref, lex)
    found_time = _find_time(low)
    if found_day is None:
        if found_time is None:
            return None
        t, phrase = found_time
        at = datetime.combine(ref.date(), t)
        if at < ref:
            at += timedelta(days=1)
        return Due(at, False, phrase)
    day, phrase, default = found_day
    if found_time is not None:
        return Due(datetime.combine(day, found_time[0]), False, f"{phrase} {found_time[1]}")
    if default is not None:
        return Due(datetime.combine(day, default), False, phrase)
    return Due(datetime.combine(day, time(0, 0)), True, phrase)
