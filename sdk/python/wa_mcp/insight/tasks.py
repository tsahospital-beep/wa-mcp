"""Rule-based detection of requests in a chat.

The other side's consecutive messages are grouped into bursts (gap from tasks.burst_gap_minutes, default 4).
Each burst gets a category and a 0-100 score, the chance that it is a real job for me:
  NEW_TASK   a new request
  FIX        a problem with something already delivered
  FOLLOW_UP  "is it done?" about an open request (raises its priority, not a new task)
  CANCEL     "never mind" for an open request
  PRECURSOR  a bare link or file: material for a request that may follow
  VOICE      voice notes without a transcript: content unknown until listened to
  CHAT       logistics, thanks, small talk
  TEAM       a request in a group that is not aimed at me
These are heuristics with reasons attached; the reading model decides with context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from wa_mcp import people
from wa_mcp.db import TimeArg
from wa_mcp.insight import base, pairing, projects
from wa_mcp.insight.base import Msg, Settings, Target

TASKISH = ("NEW_TASK", "FIX", "VOICE", "PRECURSOR")
_LIST_ITEM = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[*\u2022-])\s+\S")


@dataclass
class Verdict:
    category: str
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class Detection:
    burst: list[Msg]
    verdict: Verdict
    to_me: list[str]
    project: dict[str, Any] | None
    my_replies: list[Msg]

    @property
    def first(self) -> Msg:
        return self.burst[0]

    @property
    def last(self) -> Msg:
        return self.burst[-1]

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.burst if m.text)


def bursts(msgs: list[Msg], gap_minutes: float, include_me: bool = False) -> list[list[Msg]]:
    """Runs of messages from one sender with gaps up to gap_minutes; my messages end a run."""
    out: list[list[Msg]] = []
    cur: list[Msg] = []
    for m in msgs:
        if m.me and not include_me:
            if cur:
                out.append(cur)
                cur = []
            continue
        if cur and (m.who != cur[-1].who or m.ts - cur[-1].ts > gap_minutes * 60):
            out.append(cur)
            cur = []
        cur.append(m)
    if cur:
        out.append(cur)
    return out


def _label(m: Msg) -> str:
    if m.text:
        return m.text
    return f"[{m.kind}{': ' + m.file_name if m.file_name else ''}]"


def score_burst(burst: list[Msg], s: Settings) -> Verdict:
    """Category, score and reasons for one burst, from its words and attachments alone."""
    kw = s.task
    texts = [m.text for m in burst]
    text = "\n".join(t for t in texts if t)
    plain = base.URL_RE.sub("", text).strip()
    kinds = [m.kind for m in burst if m.kind != "text"]
    docs = [m.file_name or "document" for m in burst if m.kind == "document"]
    voices = sum(1 for m in burst if m.is_voice and not m.text)
    has_link = any(m.has_link for m in burst)
    verbs, urgent = kw["request"].find(text), kw["urgent"].find(text)
    problems, follow, cancel = kw["problem"].find(text), kw["follow_up"].find(text), kw["cancel"].find(text)
    spec = kw["spec"].find(text + " " + " ".join(docs))
    singles = [base.URL_RE.sub("", t).strip() for t in texts if t]
    numbered = any(len(_LIST_ITEM.findall(t)) >= 3 for t in texts) or (
        len(burst) <= 4 and len(_LIST_ITEM.findall(text)) >= 3
    )
    long_spec = any(len(t) > 600 for t in singles) or (len(burst) <= 4 and len(plain) > 600)
    from_voice = any(m.transcript and not m.body for m in burst)
    extra = ["read from a voice transcript"] if from_voice else []

    if cancel and kw["not_cancel"].any(text):
        cancel = []
        urgent = [u for u in urgent if u != "today"]
        extra.append("optional wording ('if not, never mind'): low priority, not a cancel")
    if cancel:
        return Verdict("CANCEL", 10, ["cancel: " + ", ".join(cancel), *extra])
    if (
        problems
        and (kw["deliverable"].any(text) or "image" in kinds or "video" in kinds)
        and len(plain) < 250
    ):
        reasons = ["problem: " + ", ".join(problems)] + (["urgency: " + ", ".join(urgent)] if urgent else [])
        return Verdict("FIX", min(85 + (5 if urgent else 0), 95), reasons + extra)
    if (docs and (spec or verbs)) or numbered or long_spec:
        reasons = (["spec file attached"] if docs else []) + (["numbered list"] if numbered else [])
        reasons += (["long brief"] if long_spec else []) + (["verb: " + ", ".join(verbs)] if verbs else [])
        return Verdict("NEW_TASK", 95, reasons + extra)
    question = "?" in plain
    if (follow and not verbs) or (verbs and question and not urgent and len(plain) < 80):
        why = (
            "status ping: " + ", ".join(follow)
            if follow
            else "question about existing work: " + ", ".join(verbs)
        )
        return Verdict("FOLLOW_UP", 20, [why, *extra])
    want = kw["want"].find(text)
    if not verbs and want and kw["deliverable"].any(text) and len(plain) >= 15:
        reasons = ["want + deliverable: " + ", ".join(want[:2] + kw["deliverable"].find(text)[:2])]
        reasons += ["urgency: " + ", ".join(urgent)] if urgent else []
        return Verdict("NEW_TASK", min(85 + (10 if urgent else 0), 97), reasons + extra)
    if verbs:
        score = 75 + (15 if urgent else 0) + (5 if len(plain) > 25 else 0) + (5 if kinds or has_link else 0)
        reasons = ["verb: " + ", ".join(verbs)] + (["urgency: " + ", ".join(urgent)] if urgent else [])
        if follow:
            score -= 10
            reasons.append("also a status ping: " + ", ".join(follow))
        return Verdict("NEW_TASK", min(score, 97), reasons + extra)
    if voices and not plain:
        return Verdict(
            "VOICE", 50 if voices == 1 else 65, [f"{voices} voice note(s) without transcript: listen"]
        )
    if not plain and (docs or kinds or has_link):
        if docs:
            return Verdict("PRECURSOR", 60, ["file without words"])
        if has_link:
            return Verdict("PRECURSOR", 35, ["bare link: often material for a later request"])
        if "sticker" in kinds and len(set(kinds)) == 1:
            return Verdict("CHAT", 2, ["sticker"])
        return Verdict("PRECURSOR", 30, ["bare " + kinds[0]])
    chatty = kw["chat"].find(text)
    if question and len(plain) < 40 and not chatty:
        return Verdict("CHAT", 20, ["short question", *extra])
    return Verdict("CHAT", 5, ["chat: " + ", ".join(chatty) if chatty else "no request markers", *extra])


def detect(msgs: list[Msg], tg: Target, s: Settings, include_me: bool = False) -> list[Detection]:
    """Score every burst of the other side in one chat (msgs oldest first)."""
    my_jids = set(people.my_jids())
    my_ids = {m.id for m in msgs if m.me}
    matcher = projects.Matcher(tg.jids, s)
    window = s.reply_window_minutes * 60
    index = {m.id: i for i, m in enumerate(msgs)}
    by_id = {m.id: m for m in msgs}
    out: list[Detection] = []
    groups = bursts(msgs, s.burst_gap_minutes, include_me)
    gi = 0
    for m in msgs:
        if m.me and not include_me:
            matcher.see(m.text + " " + m.file_name, m.ts)
        while gi < len(groups) and groups[gi][-1] is m:
            burst = groups[gi]
            gi += 1
            end = index[burst[-1].id]
            replies = [x for x in msgs[end + 1 : end + 13] if x.me and x.ts - burst[-1].ts <= window]
            out.append(_judge(burst, replies, tg, s, my_ids, my_jids, matcher, by_id))
    return out


def _judge(
    burst: list[Msg],
    replies: list[Msg],
    tg: Target,
    s: Settings,
    my_ids: set[str],
    my_jids: set[str],
    matcher: projects.Matcher,
    by_id: dict[str, Msg],
) -> Detection:
    v = score_burst(burst, s)
    accepted = next((r for r in replies if s.task["accept"].any(r.text)), None)
    if v.category in TASKISH and accepted is not None:
        v.score = min(v.score + 5, 98)
        v.reasons.append("you accepted")
    to_me = sorted({w for m in burst for w in base.addressed_to_me(m, my_ids, my_jids, s)})
    has_files = any(m.kind in ("document", "video", "voice", "audio") for m in burst)
    if (
        (v.category in ("PRECURSOR", "VOICE") or (has_files and v.category == "CHAT"))
        and accepted is not None
        and (to_me or not tg.is_group)
    ):
        v.category, v.score = "NEW_TASK", max(v.score, 82)
        v.reasons.append("files or voice sent to you and you accepted")
    if not tg.is_group:
        to_me = to_me or ["direct chat"]
    elif v.category in TASKISH:
        if to_me:
            v.score = min(v.score + 5, 98)
            v.reasons.append("aimed at you: " + ", ".join(to_me))
        elif replies:
            v.reasons.append("group message not addressed to you, but you answered")
        else:
            v.category, v.score = "TEAM", round(v.score * 0.3)
            v.reasons.append("group message not addressed to you (for someone else or the team)")
    text = "\n".join(m.text + " " + m.file_name for m in burst)
    quoted = next((m.reply_preview for m in burst if m.reply_preview), "")
    if not quoted:
        src = next((by_id[m.reply_to] for m in burst if m.reply_to in by_id), None)
        quoted = src.text if src is not None else ""
    answer = " ".join(r.text for r in replies)
    project = matcher.assign(text, quoted, burst[0].ts, answer)
    return Detection(burst, v, to_me, project, replies)


def shape(d: Detection, names: base.Names, pairs: dict[str, pairing.Pair] | None = None) -> dict[str, Any]:
    first = d.first
    out: dict[str, Any] = {
        "time": first.time,
        "chat": first.conv,
        "from": names(first.who),
        "from_jid": first.who,
        "category": d.verdict.category,
        "score": d.verdict.score,
        "reasons": d.verdict.reasons,
        "text": " / ".join(base.clip(_label(m), 300) for m in d.burst),
        "ids": [m.id for m in d.burst],
        "to_me": d.to_me,
    }
    reply = next((m for m in d.burst if m.reply_to), None)
    if reply is not None:
        out["reply_to"] = {"id": reply.reply_to, "preview": base.clip(reply.reply_preview, 160) or None}
    if d.project is not None:
        out["project"] = d.project
    if pairs is not None:
        media = [pairing.media_line(m, pairs.get(m.id)) for m in d.burst if m.kind in pairing.PAIRED_KINDS]
        if media:
            out["media"] = media
    return out


def detect_tasks(
    chat: str,
    after: TimeArg = None,
    before: TimeArg = None,
    min_score: int = 0,
    gap_minutes: float | None = None,
    include_me: bool = False,
) -> dict[str, Any]:
    """Detections for one chat: {chat, name, detections: [...] newest last}."""
    s = base.settings()
    if gap_minutes is not None:
        if gap_minutes <= 0:
            raise ValueError("gap_minutes must be positive")
        s = replace(s, burst_gap_minutes=gap_minutes)
    tg = base.target(chat)
    msgs = base.load(tg.jids, after=after, before=before, imported=s.include_imported)
    pairs = pairing.pair_media(msgs)
    names = base.Names()
    dets = [d for d in detect(msgs, tg, s, include_me) if d.verdict.score >= min_score]
    counts: dict[str, int] = {}
    for d in dets:
        counts[d.verdict.category] = counts.get(d.verdict.category, 0) + 1
    return {
        "chat": tg.key,
        "name": tg.name,
        "is_group": tg.is_group,
        "counts": counts,
        "detections": [shape(d, names, pairs) for d in dets],
    }
