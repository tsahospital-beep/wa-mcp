"""Link photos and files to the words around them, so a list can say what an image refers to.

Words belong to a photo or file when they are its caption, come from the same sender within a few
minutes with nobody else writing anything substantial in between, or reply to it. A run of photos
shares the words that follow it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wa_mcp.insight.base import Msg, clip

PAIRED_KINDS = ("image", "video", "document")


@dataclass
class Pair:
    media_id: str
    kind: str
    texts: list[str] = field(default_factory=list)
    text_ids: list[str] = field(default_factory=list)
    how: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " / ".join(self.texts)

    def as_dict(self) -> dict[str, object]:
        return {"media_id": self.media_id, "kind": self.kind, "text": clip(self.text, 300), "how": self.how}


def pair_media(msgs: list[Msg], gap_seconds: int = 300, max_after: int = 3) -> dict[str, Pair]:
    """{media message id: Pair} for one chat, msgs oldest first."""
    out: dict[str, Pair] = {}
    for i, m in enumerate(msgs):
        if m.kind not in PAIRED_KINDS:
            continue
        p = Pair(m.id, m.kind)
        if m.body:
            p.texts.append(m.body)
            p.text_ids.append(m.id)
            p.how.append("caption")
        after = 0
        for x in msgs[i + 1 :]:
            if x.ts - m.ts > gap_seconds:
                break
            if x.who != m.who:
                if x.kind != "text" or len(x.text) > 40:
                    break
                continue
            if x.kind != "text":
                continue
            if x.text:
                p.texts.append(x.text)
                p.text_ids.append(x.id)
                p.how.append("after")
                after += 1
                if after >= max_after:
                    break
        if not m.body and not after:
            for x in reversed(msgs[:i]):
                if x.who != m.who or m.ts - x.ts > gap_seconds:
                    break
                if x.kind != "text":
                    continue
                if x.text:
                    p.texts.insert(0, x.text)
                    p.text_ids.insert(0, x.id)
                    p.how.insert(0, "before")
                    break
        for x in msgs[i + 1 :]:
            if x.reply_to == m.id and x.text:
                p.texts.append(x.text)
                p.text_ids.append(x.id)
                p.how.append("reply")
        if p.texts:
            out[m.id] = p
    return out


def media_line(m: Msg, pair: Pair | None) -> str:
    """Short line for list views: what the file is and what was said about it."""
    head = m.kind + (f" {m.file_name}" if m.file_name else "")
    if pair is not None and pair.texts:
        return f"{head}: about '{clip(pair.text, 120)}'"
    if m.file_text:
        return f"{head}: contains '{clip(m.file_text, 120)}'"
    return head
