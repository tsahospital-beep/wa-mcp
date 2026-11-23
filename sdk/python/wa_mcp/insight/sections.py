"""Behaviour sections of the chat analytics: volume, timing, length, emoji, stickers, media, voice, quotes.

Each section takes a Chat and returns {n, low_data, data, examples}. Examples are masked (phone numbers,
links, mail) and never contain secret-looking text.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from statistics import median as stat_median

from wa_mcp import db, people
from wa_mcp.insight import base, pairing, style
from wa_mcp.insight.base import ME, THEM, Msg, Settings, median, pct, per100, share
from wa_mcp.insight.chat import BUCKETS, SIDES, WEEKDAYS, Chat, Data, Pair, Turn, sec


def _months(cx: Chat) -> list[str]:
    if not cx.msgs:
        return []
    a, b = cx.msgs[0].dt, cx.msgs[-1].dt
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def volume(cx: Chat) -> Data:
    msgs = cx.msgs
    by = Counter(m.side for m in msgs)
    chars: Counter[str] = Counter()
    for m in msgs:
        chars[m.side] += len(m.plain)
    turns = Counter(t.side for t in cx.turns)
    months: dict[str, Counter[str]] = defaultdict(Counter)
    hours: dict[int, Counter[str]] = defaultdict(Counter)
    wdays: dict[int, Counter[str]] = defaultdict(Counter)
    for m in msgs:
        months[m.dt.strftime("%Y-%m")][m.side] += 1
        hours[m.dt.hour][m.side] += 1
        wdays[m.dt.weekday()][m.side] += 1
    mlist = _months(cx)
    totals = [sum(months[k].values()) for k in mlist]
    gaps = []
    for i, k in enumerate(mlist):
        near = [totals[j] for j in range(max(0, i - 2), min(len(mlist), i + 3)) if j != i and totals[j] > 0]
        if 0 < i < len(mlist) - 1 and len(near) >= 2 and totals[i] < 0.25 * stat_median(near):
            gaps.append(k)
    silences = sorted(((b.ts - a.ts, a, b) for a, b in itertools.pairwise(msgs)), key=lambda x: -x[0])[:3]
    started = Counter(s.side for s in cx.sessions)
    data: Data = {
        "messages": {"me": by[ME], "them": by[THEM], "total": len(msgs)},
        "my_share": share(by[ME], len(msgs)),
        "turns": {"me": turns[ME], "them": turns[THEM]},
        "chars": {"me": chars[ME], "them": chars[THEM]},
        "msgs_per_turn": {s: share(by[s], turns[s]) for s in SIDES},
        "sessions": len(cx.sessions),
        "sessions_started": {"me": started[ME], "them": started[THEM], "my_share": share(started[ME], len(cx.sessions))},
        "by_month": {k: {"me": months[k][ME], "them": months[k][THEM]} for k in mlist},
        "by_hour": {f"{h:02d}": {"me": hours[h][ME], "them": hours[h][THEM]} for h in range(24) if hours[h]},
        "by_weekday": {WEEKDAYS[d]: {"me": wdays[d][ME], "them": wdays[d][THEM]} for d in range(7)},
        "peak_hours": [f"{h:02d}" for _, h in sorted(((sum(v.values()), h) for h, v in hours.items()), reverse=True)[:3]],
        "longest_silences": [
            {"from": a.time, "to": b.time, "days": round(gap / 86400, 1)} for gap, a, b in silences if gap >= 86400
        ],
        "possible_history_gaps": gaps,
        "period": [msgs[0].time if msgs else None, msgs[-1].time if msgs else None],
    }  # fmt: skip
    if cx.tg.is_group:
        who = Counter(cx.label(s.who) for s in cx.sessions)
        data["sessions_started_by"] = dict(who.most_common(5))
    return sec(len(msgs), data)


def _lat_row(xs: list[float]) -> Data:
    m = median(xs, 2)
    return {"median_min": m, "p75_min": pct(xs, 75, 2), "p90_min": pct(xs, 90, 1), "n": len(xs)}


def _block(h: int) -> str:
    return "night" if h < 6 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"


def reply_speed(cx: Chat) -> Data:
    pairs = cx.pairs()
    buckets: Counter[str] = Counter()
    for p in pairs:
        if p.night:
            buckets["night_start"] += 1
        elif p.minutes is None:
            buckets["no_reply"] += 1
        else:
            buckets[next(name for name, lim in BUCKETS if p.minutes <= lim)] += 1
    lats = [p.minutes for p in pairs if p.minutes is not None and not p.night]
    by_block: dict[str, list[float]] = defaultdict(list)
    by_day: dict[str, list[float]] = defaultdict(list)
    for p in pairs:
        if p.minutes is None or p.night:
            continue
        dt = datetime.fromtimestamp(p.incoming.end)
        by_block[_block(dt.hour)].append(p.minutes)
        by_day["weekend" if dt.weekday() >= 5 else "weekday"].append(p.minutes)
    my_turns = [t for t in cx.turns if t.side == ME]
    night_mine = [t for t in my_turns if cx.night(t.start)]
    data: Data = {
        "me": _lat_row(lats),
        "them": _lat_row(cx._theirs) if not cx.tg.is_group else None,
        "buckets": {k: buckets.get(k, 0) for k in [b for b, _ in BUCKETS] + ["no_reply", "night_start"]},
        "share_within_5m": share(buckets.get("under_5m", 0), len(lats)),
        "by_time_of_day": {k: median(v, 2) for k, v in sorted(by_block.items())},
        "weekday_vs_weekend": {k: median(v, 2) for k, v in sorted(by_day.items())},
        "night_replies": {
            "count": len(night_mine),
            "share_of_my_turns": share(len(night_mine), len(my_turns)),
        },
        "note": "no 24 h cut; night_start: their message came at night and the reply was not immediate"
        + ("; group: only messages aimed at me" if cx.tg.is_group else ""),
    }
    slow = sorted(
        [p for p in pairs if p.minutes is not None and not p.night], key=lambda p: -(p.minutes or 0)
    )
    exs = [
        cx.ex(p.incoming.msgs[-1], p.incoming.text, reply_after=base.fmt_minutes(p.minutes))
        for p in slow[: cx.n_ex]
    ]
    return sec(len(pairs), data, exs)


def length(cx: Chat) -> Data:
    out: Data = {}
    for side in SIDES:
        rs = [m for m in cx.msgs if m.side == side and m.body]
        ts = [t for t in cx.turns if t.side == side]
        lines = [len([ln for ln in t.text.split("\n") if ln.strip()]) for t in ts if t.text]
        runs: list[int] = []
        for t in ts:
            run = 1
            for a, b in itertools.pairwise(t.msgs):
                if b.ts - a.ts <= 120:
                    run += 1
                    continue
                if run >= 3:
                    runs.append(run)
                run = 1
            if run >= 3:
                runs.append(run)
        words = [len(m.plain.split()) for m in rs]
        out[side] = {
            "chars_median": median([len(m.plain) for m in rs]),
            "chars_p90": pct([len(m.plain) for m in rs], 90),
            "words_median": median(words),
            "words_p90": pct(words, 90),
            "lines_per_turn_median": median(lines),
            "msgs_per_turn_median": median([len(t.msgs) for t in ts]),
            "bursts_per_100_turns": per100(len(runs), len(ts)),
            "text_messages": len(rs),
        }
    med = out[ME]["words_median"] or 0
    typical = [m for m in cx.msgs if m.me and m.body and abs(len(m.plain.split()) - med) <= 1]
    exs = [cx.ex(m, words=len(m.plain.split())) for m in typical[-cx.n_ex :]]
    return sec(out[ME]["text_messages"] + out[THEM]["text_messages"], out, exs)


def emoji(cx: Chat) -> Data:
    out: Data = {}
    exs = []
    for side in SIDES:
        rs = [m for m in cx.msgs if m.side == side and m.body]
        cnt: Counter[str] = Counter()
        with_e = at_end = at_start = only = laugh = 0
        for m in rs:
            if base.is_laugh(m.body, cx.s):
                laugh += 1
            cl = base.emoji_clusters(m.body)
            if not cl:
                continue
            with_e += 1
            cnt.update(cl)
            rest = m.body
            for e in cl:
                rest = rest.replace(e, "")
            stripped = m.body.strip()
            if not re.sub(r"[\s\u200d\ufe0f]", "", rest):
                only += 1
            elif re.sub(r"[\s.?]+$", "", stripped).endswith(cl[-1]):
                at_end += 1
            elif stripped.startswith(cl[0]):
                at_start += 1
        out[side] = {
            "per_100_messages": per100(sum(cnt.values()), len(rs)),
            "share_messages_with_emoji": share(with_e, len(rs)),
            "top": [[e, c] for e, c in cnt.most_common(10)],
            "position": {"end": at_end, "start": at_start, "emoji_only": only},
            "laugh_per_100": per100(laugh, len(rs)),
            "text_messages": len(rs),
        }
        last = next(
            (m for m in reversed(rs) if base.emoji_clusters(m.body) and len(m.plain.split()) >= 2), None
        )
        if last is not None:
            exs.append(cx.ex(last))
    return sec(out[ME]["text_messages"] + out[THEM]["text_messages"], out, exs)


def _situation(m: Msg | None, s: Settings) -> str:
    if m is None:
        return "none"
    if m.kind == "sticker":
        return "sticker"
    if m.kind != "text":
        return "media"
    t = m.text
    if "?" in t:
        return "question"
    if s.task["thanks"].any(t):
        return "thanks"
    if base.is_laugh(t, s):
        return "laugh"
    if s.words["greeting"].any(t):
        return "greeting"
    if s.words["farewell"].any(t):
        return "farewell"
    return "text"


def stickers(cx: Chat) -> Data:
    msgs = cx.msgs
    idx = [i for i, m in enumerate(msgs) if m.kind == "sticker"]
    per: dict[str, Data] = {}
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    exs: list[Data | None] = []
    for i in idx:
        m = msgs[i]
        sid = m.file_name or (f"size-{m.byte_size}" if m.byte_size else m.id)
        p = per.setdefault(sid, {"sticker": sid, "n": 0, "by": Counter(), "first": m.time, "last": m.time})
        p["n"] += 1
        p["by"][m.side] += 1
        p["last"] = m.time
        prev = next(
            (x for x in reversed(msgs[max(0, i - 8) : i]) if x.side != m.side and m.ts - x.ts <= 1800), None
        )
        nxt = next((x for x in msgs[i + 1 : i + 10] if x.side != m.side and x.ts - m.ts <= 1800), None)
        before[f"{m.side}:{_situation(prev, cx.s)}"] += 1
        after[f"{m.side}:{_situation(nxt, cx.s) if nxt is not None else 'nothing'}"] += 1
        if prev is not None and prev.text and len(exs) < cx.n_ex:
            exs.append(cx.ex(prev, sticker_from=cx.label(m.who), sticker=sid[:24]))
    first = msgs[idx[0]].ts if idx else None
    rate = {}
    for side in SIDES:
        since = [m for m in msgs if m.side == side and first is not None and m.ts >= first]
        n = sum(1 for m in since if m.kind == "sticker")
        rate[side] = {"messages": len(since), "stickers": n, "per_100": per100(n, len(since))}
    top = sorted(per.values(), key=lambda p: -int(p["n"]))[:15]
    data: Data = {
        "total": len(idx),
        "by_side": dict(Counter(msgs[i].side for i in idx)),
        "unique": len(per),
        "per_100_since_first_sticker": rate,
        "first_sticker": msgs[idx[0]].time if idx else None,
        "before": dict(before.most_common()),
        "after": dict(after.most_common()),
        "top": [dict(p, by=dict(p["by"])) for p in top],
        "note": "rates count from the first sticker stored for this chat; stickers are told apart by file name or size",
    }
    return sec(len(idx), data, exs, low=5)


def reactions(cx: Chat) -> Data:
    """Reactions given and received per side (emoji kept as stored) and the people who react most."""
    found = db.reactions_many([(m.conv, m.id) for m in cx.msgs])
    given: dict[str, Counter[str]] = {side: Counter() for side in SIDES}
    received: dict[str, Counter[str]] = {side: Counter() for side in SIDES}
    reactors: Counter[str] = Counter()
    total = 0
    for m in cx.msgs:
        for r in found.get((m.conv, m.id), []):
            jids = people.same_person(str(r["reactor"]))
            side = ME if any(j in cx.my_jids for j in jids) else THEM
            emoji = str(r["emoji"])
            given[side][emoji] += 1
            received[m.side][emoji] += 1
            reactors[ME if side == ME else cx.names(jids[0])] += 1
            total += 1
    data: Data = {
        "total": total,
        "given": {side: sum(c.values()) for side, c in given.items()},
        "received": {side: sum(c.values()) for side, c in received.items()},
        "emoji_given": {side: dict(c.most_common(10)) for side, c in given.items()},
        "emoji_received": {side: dict(c.most_common(10)) for side, c in received.items()},
        "top_reactors": [{"who": who, "n": n} for who, n in reactors.most_common(5)],
    }
    return sec(total, data, low=5)


def media(cx: Chat) -> Data:
    by: dict[str, Counter[str]] = defaultdict(Counter)
    months: dict[str, Counter[str]] = defaultdict(Counter)
    exts: Counter[str] = Counter()
    files = [m for m in cx.msgs if m.kind != "text"]
    for m in files:
        by[m.side][m.kind] += 1
        months[m.dt.strftime("%Y-%m")][m.kind] += 1
        if m.kind == "document":
            ext = re.search(r"\.([A-Za-z0-9]{1,6})$", m.file_name or "")
            exts[ext.group(1).lower() if ext else "?"] += 1
    pairs = pairing.pair_media(cx.msgs)
    imgs = [m for m in files if m.kind == "image"]
    docs = [m for m in files if m.kind == "document"]
    data: Data = {
        "by_side": {s: dict(c) for s, c in by.items()},
        "document_types": dict(exts.most_common(10)),
        "downloaded": {"n": sum(1 for m in files if m.local_path), "of": len(files)},
        "text_extracted": {
            "documents": f"{sum(1 for m in docs if m.file_text)}/{len(docs)}",
            "images": f"{sum(1 for m in imgs if m.file_text)}/{len(imgs)}",
        },
        "images_with_words_nearby": f"{sum(1 for m in imgs if m.id in pairs)}/{len(imgs)}",
        "links": {s: sum(1 for m in cx.msgs if m.side == s and m.has_link) for s in SIDES},
        "by_month": {k: dict(v) for k, v in sorted(months.items())},
    }
    exs = [
        cx.ex(m, pairing.media_line(m, pairs.get(m.id)))
        for m in [m for m in files if m.id in pairs][-cx.n_ex :]
    ]
    return sec(len(files), data, exs)


def voice(cx: Chat) -> Data:
    notes = [m for m in cx.msgs if m.kind in ("voice", "audio")]
    tr = [m for m in notes if m.transcript]
    langs = Counter(
        (m.transcript_lang or "unknown")
        + ("" if m.transcript_conf is None or m.transcript_conf >= 0.6 else ":low")
        for m in tr
    )
    secs = [m.byte_size / 2000.0 for m in notes if m.byte_size]
    my_turns = [t for t in cx.turns if t.side == ME]
    voice_only = sum(1 for t in my_turns if t.kinds and all(k == "voice" for k in t.kinds) and not t.text)
    mine = sum(1 for m in cx.msgs if m.me)
    data: Data = {
        "by_side": dict(Counter(m.side for m in notes)),
        "by_kind": dict(Counter(m.kind for m in notes)),
        "transcribed": len(tr),
        "untranscribed": len(notes) - len(tr),
        "transcribed_share": share(len(tr), len(notes)),
        "languages": dict(langs.most_common(6)),
        "minutes_total_estimate": round(sum(secs) / 60, 1) if secs else None,
        "seconds_median_estimate": median(secs, 0) if secs else None,
        "my_voice_share": share(sum(1 for m in notes if m.me), mine),
        "my_voice_only_turns_share": share(voice_only, len(my_turns)),
        "untranscribed_ids": [m.id for m in notes if not m.transcript][:20],
        "note": "durations are estimated from file size (about 2 KB per second of opus)",
    }
    exs = [cx.ex(m, m.transcript, source="voice", lang=m.transcript_lang) for m in tr[-cx.n_ex :]]
    return sec(len(notes), data, exs, low=5)


def quotes(cx: Chat) -> Data:
    q = [m for m in cx.msgs if m.reply_to]
    side_n = Counter(m.side for m in cx.msgs)
    own: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    pairs_: Counter[tuple[str, str]] = Counter()
    delays: list[float] = []
    exs: list[Data | None] = []
    for m in q:
        src = cx.by_id.get(m.reply_to)
        if src is None:
            own[f"{m.side}:unknown"] += 1
            continue
        own[f"{m.side}:{'own' if src.who == m.who else 'other'}"] += 1
        kinds[src.kind] += 1
        d = (m.ts - src.ts) / 60
        if d >= 0:
            delays.append(d)
        if src.who != m.who:
            pairs_[(cx.label(m.who), cx.label(src.who))] += 1
        if len(exs) < cx.n_ex:
            exs.append(cx.ex(m, quoted=base.safe_text(src.text or src.kind, 50), delay=base.fmt_minutes(d)))
    data: Data = {
        "share": {s: share(sum(1 for m in q if m.side == s), side_n[s], 3) for s in SIDES},
        "n": {s: sum(1 for m in q if m.side == s) for s in SIDES},
        "own_vs_other": dict(own),
        "quoted_kind": dict(kinds),
        "delay_median_min": median(delays),
        "delay_p90_min": pct(delays, 90),
        "older_than_24h": sum(1 for d in delays if d > 1440),
    }
    if cx.tg.is_group:
        data["who_quotes_whom"] = [[a, b, c] for (a, b), c in pairs_.most_common(5)]
    return sec(len(q), data, exs)


def _turn_kind(t: Turn) -> str:
    if "voice" in t.kinds:
        return "voice"
    if "document" in t.kinds:
        return "document"
    if any(k in ("image", "video") for k in t.kinds):
        return "photo_or_video"
    return "question" if "?" in t.text else "text"


def deferrals(cx: Chat) -> Data:
    pairs = [p for p in cx.pairs() if not p.night]
    deferred = [p for p in pairs if p.minutes is None or p.minutes > 180]
    quick = [p for p in pairs if p.minutes is not None and p.minutes <= 5]
    avg = share(len(deferred), len(pairs)) or 0
    cats: dict[str, list[Pair]] = defaultdict(list)
    for p in pairs:
        cats[_turn_kind(p.incoming)].append(p)
    dset = {id(p) for p in deferred}
    by_kind: list[Data] = []
    for k, ps in cats.items():
        d = sum(1 for p in ps if id(p) in dset)
        sh = d / len(ps)
        by_kind.append(
            {
                "kind": k,
                "n": len(ps),
                "deferred": d,
                "share": round(sh, 2),
                "x_vs_avg": round(sh / avg, 2) if avg else None,
            }
        )
    by_kind.sort(key=lambda c: (-float(c["x_vs_avg"] or 0), -int(c["n"])))
    phrases = []
    for t in cx.turns:
        if t.side == ME and t.text and cx.s.words["deferral"].any(t.text):
            phrases.append((t, cx.substantive_after(t.end)))
    read_late = 0
    read_delays = [(m.read_at - m.ts) / 60 for m in cx.msgs if not m.me and m.read_at and m.read_at >= m.ts]
    for p in deferred:
        last = p.incoming.msgs[-1]
        if last.read_at and (p.reply is None or last.read_at < p.reply.start):
            read_late += 1
    data: Data = {
        "deferred": len(deferred),
        "immediate": len(quick),
        "share_deferred": share(len(deferred), len(pairs)),
        "by_incoming_kind": by_kind,
        "deferral_phrases": len(phrases),
        "deferral_phrases_followed_up_72h": sum(1 for _, ok in phrases if ok),
        "deferred_but_read": read_late,
        "read_delay_median_min": median(read_delays),
        "note": "deferred: reply after more than 3 h or none (night starts excluded); read times exist only for "
        "messages this account marked read",
    }
    exs = [
        cx.ex(p.incoming.msgs[-1], p.incoming.text, reply_after=base.fmt_minutes(p.minutes)) for p in deferred
    ]
    exs += [cx.ex(t.msgs[0], t.text, phrase=True, followed_up_72h=ok) for t, ok in phrases]
    return sec(len(pairs), data, exs[-cx.n_ex :])


SECTIONS: dict[str, Callable[[Chat], Data]] = {
    "volume": volume,
    "reply_speed": reply_speed,
    "length": length,
    "emoji": emoji,
    "stickers": stickers,
    "reactions": reactions,
    "media": media,
    "voice": voice,
    "quotes": quotes,
    "deferrals": deferrals,
    "formality": style.formality,
    "script": style.script,
    "topics_tasks": style.topics_tasks,
    "group_roles": style.group_roles,
}
NAMES = (*SECTIONS, "phrases", "capture_gaps")
