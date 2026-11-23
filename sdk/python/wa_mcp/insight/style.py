"""Language and role sections of the chat analytics: formality, scripts, requests and promises,
phrases typical for a chat, group roles and what the bridge cannot see.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from wa_mcp.insight import base, when
from wa_mcp.insight.base import ME, THEM, Msg, Settings, share
from wa_mcp.insight.chat import SIDES, Chat, Data, Turn, sec


def formality(cx: Chat) -> Data:
    s = cx.s
    out: Data = {}
    for side in SIDES:
        rs = [m for m in cx.msgs if m.side == side and m.body]
        formal = [m for m in rs if s.words["formal"].any(m.body)]
        informal = [m for m in rs if s.words["informal"].any(m.body)]
        out[side] = {
            "formal_share": share(len(formal), len(rs)),
            "informal_share": share(len(informal), len(rs)),
            "formal": len(formal),
            "informal": len(informal),
            "text_messages": len(rs),
        }
    follow = []
    for t in cx.turns:
        if t.side != ME or t.i == 0 or cx.turns[t.i - 1].side == ME or not t.text:
            continue
        prev = cx.turns[t.i - 1]
        if prev.text:
            follow.append((s.words["formal"].any(prev.text), s.words["formal"].any(t.text)))
    out["mirror"] = {
        "my_formal_after_formal": share(sum(1 for a, b in follow if a and b), sum(1 for a, _ in follow if a)),
        "my_formal_after_informal": share(
            sum(1 for a, b in follow if not a and b), sum(1 for a, _ in follow if not a)
        ),
    }
    out["note"] = "marker words come from insight.words.formal and insight.words.informal"
    mine = [m for m in cx.msgs if m.me and m.body and s.words["formal"].any(m.body)]
    return sec(
        out[ME]["text_messages"] + out[THEM]["text_messages"], out, [cx.ex(m) for m in mine[-cx.n_ex :]]
    )


def script(cx: Chat) -> Data:
    def shares(ms: list[Msg]) -> tuple[Data, int]:
        c = Counter(base.script_of(m.body) for m in ms)
        c.pop("none", None)
        total = sum(c.values())
        return {k: share(v, total) for k, v in c.most_common()}, total

    out: Data = {}
    for side in SIDES:
        rs = [m for m in cx.msgs if m.side == side and m.body]
        sh, n = shares(rs)
        mixed = sum(1 for m in rs if sum(1 for v in base.script_counts(m.body).values() if v >= 3) >= 2)
        out[side] = {
            "scripts": sh,
            "n": n,
            "main": next(iter(sh), None),
            "mixed_share": share(mixed, len(rs)),
        }
    months: dict[str, list[Msg]] = defaultdict(list)
    for m in cx.msgs:
        if m.me and m.body:
            months[m.dt.strftime("%Y-%m")].append(m)
    out["my_by_month"] = {k: shares(v)[0] for k, v in sorted(months.items())}
    mirror = []
    for t in cx.turns:
        if t.side != ME or t.i == 0 or cx.turns[t.i - 1].side == ME:
            continue
        a = next(
            (base.script_of(m.body) for m in t.msgs if m.body and base.script_of(m.body) != "none"), None
        )
        prev = cx.turns[t.i - 1].msgs
        b = next(
            (base.script_of(m.body) for m in reversed(prev) if m.body and base.script_of(m.body) != "none"),
            None,
        )
        if a and b:
            mirror.append(a == b)
    out["mirror_rate"] = share(sum(mirror), len(mirror))
    out["mirror_n"] = len(mirror)
    out["voice_languages"] = dict(
        Counter(
            f"{m.side}:{m.transcript_lang}" for m in cx.msgs if m.transcript and m.transcript_lang
        ).most_common(8)
    )
    out["note"] = (
        "scripts count letters per Unicode block; mirror_rate: my reply uses the script of their last turn"
    )
    exs: list[Data | None] = []
    for sc in list(out[ME]["scripts"])[:3]:
        hit = next((m for m in reversed(cx.msgs) if m.me and m.body and base.script_of(m.body) == sc), None)
        if hit is not None:
            exs.append(cx.ex(hit, script=sc))
    return sec(out[ME]["n"] + out[THEM]["n"], out, exs)


def topics_tasks(cx: Chat) -> Data:
    s = cx.s
    dets = cx.detections()
    cats = Counter(d.verdict.category for d in dets)
    strong = [
        d for d in dets if d.verdict.category in ("NEW_TASK", "FIX") and d.verdict.score >= s.min_task_score
    ]
    asks, promises = [], []
    for t in cx.turns:
        if not t.text:
            continue
        if t.side == THEM and (s.words["ask"].any(t.text) or "?" in t.text):
            asks.append((t, cx.substantive_after(t.end)))
        if t.side == ME and s.words["promise"].any(t.text):
            dated = when.parse_due(t.text, t.msgs[0].dt) is not None
            promises.append((t, dated, cx.substantive_after(t.end)))
    projects = Counter(str(d.project["name"]) for d in strong if d.project and d.project.get("id"))
    data: Data = {
        "detections": dict(cats.most_common()),
        "requests": len(strong),
        "requests_by_project": dict(projects.most_common(8)),
        "asks": {"n": len(asks), "followed_up_72h": sum(1 for _, ok in asks if ok)},
        "promises": {
            "n": len(promises),
            "with_date": sum(1 for _, dated, _ in promises if dated),
            "followed_up_72h": sum(1 for _, _, ok in promises if ok),
        },
        "note": "requests: NEW_TASK and FIX bursts at or above tasks.min_score; followed_up_72h: a file, link, "
        "'done' or a longer message from me within 72 h",
    }
    exs = [
        cx.ex(d.first, d.text, category=d.verdict.category, score=d.verdict.score) for d in strong[-cx.n_ex :]
    ]
    exs += [cx.ex(t.msgs[0], t.text, kind="promise", dated=dated) for t, dated, _ in promises[-2:]]
    return sec(len(cx.turns), data, exs)


@dataclass
class Baseline:
    """n-gram use in my other chats: per-message counts for each side and chat counts per word."""

    me: Counter[str]
    them: Counter[str]
    n_me: int
    n_them: int
    chat_df: Counter[str]
    n_chats: int


def ngrams(text: str, stop: frozenset[str], nmax: int = 3) -> set[str]:
    toks = base.tokens(text)
    out = set()
    for n in range(1, nmax + 1):
        for i in range(len(toks) - n + 1):
            g = toks[i : i + n]
            if all(w in stop for w in g):
                continue
            out.add(" ".join(g))
    return out


def build_baseline(msgs_by_chat: dict[str, list[Msg]], s: Settings) -> Baseline:
    me: Counter[str] = Counter()
    them: Counter[str] = Counter()
    df: Counter[str] = Counter()
    n_me = n_them = 0
    stop = frozenset(s.words["stopwords"].items)
    for items in msgs_by_chat.values():
        seen: set[str] = set()
        for m in items:
            if not m.body or base.looks_secret(m.body):
                continue
            gs = ngrams(m.body, stop)
            if m.me:
                me.update(gs)
                n_me += 1
            else:
                them.update(gs)
                n_them += 1
            seen |= {g for g in gs if " " not in g}
        df.update(seen)
    return Baseline(me, them, n_me, n_them, df, len(msgs_by_chat))


def phrases(cx: Chat, base_line: Baseline) -> Data:
    s = cx.s
    stop = frozenset(s.words["stopwords"].items)
    here: dict[str, Counter[str]] = {ME: Counter(), THEM: Counter()}
    sample: dict[tuple[str, str], Turn] = {}
    turns_n: Counter[str] = Counter()
    name_toks = set(base.tokens(cx.tg.name)) | {
        w for who in {t.who for t in cx.turns} for w in base.tokens(cx.label(who))
    }
    for t in cx.turns:
        if not t.text or base.looks_secret(t.text):
            continue
        turns_n[t.side] += 1
        gs = ngrams(t.text, stop)
        here[t.side].update(gs)
        for g in gs:
            sample.setdefault((t.side, g), t)

    def usable(g: str) -> bool:
        ws = g.split()
        if any(w in name_toks for w in ws) or base.is_laugh(g, s):
            return False
        return len(ws) > 1 or (len(ws[0]) >= 4 and ws[0] not in stop)

    nch = max(2, base_line.n_chats)

    def distinct(side: str) -> list[tuple[str, int, float]]:
        other, n_other = (base_line.me, base_line.n_me) if side == ME else (base_line.them, base_line.n_them)
        out = []
        for g, c in here[side].items():
            if c < 3 or not usable(g):
                continue
            ratio = (c / max(1, turns_n[side])) / ((other.get(g, 0) + 0.5) / max(1, n_other))
            if ratio < 2.0:
                continue
            idf = math.log((nch + 1) / (base_line.chat_df.get(g.split()[0], 0) + 0.5))
            out.append(
                (g, c, round(c * max(idf, 0.1) * min(ratio, 20) ** 0.5 * (1.6 if " " in g else 1.0), 1))
            )
        out.sort(key=lambda x: (-x[2], x[0]))
        kept: list[tuple[str, int, float]] = []
        for g, c, sc in out:
            if any(g in k and abs(c - kc) <= 1 for k, kc, _ in kept):
                continue
            kept = [(k, kc, ks) for k, kc, ks in kept if not (k in g and abs(c - kc) <= 1)]
            kept.append((g, c, sc))
        return kept[:12]

    def row(side: str, g: str, c: int, score: float) -> Data:
        t = sample.get((side, g))
        return {
            "phrase": base.mask(g),
            "n": c,
            "score": score,
            "example": base.safe_text(t.text, 90) if t else None,
        }

    shared = sorted(
        (g for g, c in here[THEM].items() if c >= 3 and here[ME].get(g, 0) >= 3 and usable(g)),
        key=lambda g: (-len(g.split()), -here[ME][g]),
    )[:10]
    data: Data = {
        "mine": [row(ME, g, c, sc) for g, c, sc in distinct(ME)],
        "theirs": [row(THEM, g, c, sc) for g, c, sc in distinct(THEM)],
        "shared": [{"phrase": base.mask(g), "n": min(here[ME][g], here[THEM][g])} for g in shared],
        "baseline": {
            "my_messages": base_line.n_me,
            "their_messages": base_line.n_them,
            "chats": base_line.n_chats,
        },
        "method": "1 to 3 word phrases used at least 3 times here and at least twice as often as in my other chats "
        "(mine against my messages elsewhere, theirs against other people); names and stop words left out",
    }
    exs = [
        cx.ex(sample[(ME, g)].msgs[0], sample[(ME, g)].text, phrase=g) for g, _, _ in distinct(ME)[: cx.n_ex]
    ]
    return sec(turns_n[ME] + turns_n[THEM], data, exs)


def group_roles(cx: Chat) -> Data:
    if not cx.tg.is_group:
        return sec(0, {"note": "only for groups"}, low=0)
    s = cx.s
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    total = len(cx.msgs) or 1
    for m in cx.msgs:
        st = stats[m.who]
        st["messages"] += 1
        st["media"] += m.kind != "text"
        st["laughs"] += bool(m.body and base.is_laugh(m.body, s))
        st["asks"] += bool(
            not m.me and m.text and (s.words["ask"].any(m.text) or s.task["request"].any(m.text))
        )
        st["to_me"] += bool(not m.me and base.addressed_to_me(m, cx.my_ids, cx.my_jids, s))
    requests = Counter(
        d.first.who
        for d in cx.detections()
        if d.verdict.category in ("NEW_TASK", "FIX", "TEAM") and d.verdict.score >= 20
    )
    parts = []
    for who, st in sorted(stats.items(), key=lambda kv: -kv[1]["messages"]):
        sh = st["messages"] / total
        roles = []
        if st["asks"] >= 5 or requests.get(who, 0) >= 2:
            roles.append("asks for things")
        if st["media"] >= 0.4 * st["messages"] and st["messages"] >= 10:
            roles.append("shares media")
        if sh < 0.02:
            roles.append("quiet")
        parts.append(
            {"who": cx.label(who), "messages": st["messages"], "share": round(sh, 3), "media": st["media"],
             "laughs": st["laughs"], "asks": st["asks"], "aimed_at_me": st["to_me"], "requests": requests.get(who, 0),
             "roles": roles or ["member"]}
        )  # fmt: skip
    aimed = answered = 0
    replies_to: Counter[str] = Counter()
    for i, t in enumerate(cx.turns):
        if t.side == ME:
            src = next((cx.by_id[m.reply_to] for m in t.msgs if m.reply_to in cx.by_id), None)
            if src is not None and not src.me:
                replies_to[cx.label(src.who)] += 1
            continue
        if cx.aimed(t):
            aimed += 1
            answered += any(x.side == ME and x.start - t.end <= 1800 for x in cx.turns[i + 1 : i + 15])
    data: Data = {
        "participants": parts[:15],
        "participant_count": len(parts),
        "aimed_at_me": aimed,
        "answered_when_aimed_30m": share(answered, aimed),
        "i_reply_to": dict(replies_to.most_common(6)),
    }
    return sec(len(cx.msgs), data)


CAPTURE_GAPS = (
    ("polls", "polls are not stored"),
    ("locations", "shared locations are not stored"),
    ("contacts", "shared contact cards are not stored"),
    ("view_once", "view-once media is not stored"),
    ("their_read_receipts", "when the other side read my messages is not stored"),
    ("forwarded", "the forwarded flag is not stored"),
    ("voice_length", "voice note length is not stored (estimated from size)"),
    ("system_events", "joins, leaves and subject changes are not stored"),
)


def capture_gaps(cx: Chat, history: dict[str, Any] | None = None) -> Data:
    notes = [m for m in cx.msgs if m.kind in ("voice", "audio")]
    imgs = [m for m in cx.msgs if m.kind == "image"]
    docs = [m for m in cx.msgs if m.kind == "document"]
    files = [m for m in cx.msgs if m.kind != "text"]
    data: Data = {
        "not_captured": [{"what": k, "why": v} for k, v in CAPTURE_GAPS],
        "coverage": {
            "voice_transcripts": f"{sum(1 for m in notes if m.transcript)}/{len(notes)}",
            "image_text": f"{sum(1 for m in imgs if m.file_text)}/{len(imgs)}",
            "document_text": f"{sum(1 for m in docs if m.file_text)}/{len(docs)}",
            "media_downloaded": f"{sum(1 for m in files if m.local_path)}/{len(files)}",
        },
        "history_sync": history or {},
        "hint": "older messages: sync_chat_history; transcripts: transcribe_chat_audio; file text: extract_file_text",
    }
    return sec(len(CAPTURE_GAPS), data, low=0)
