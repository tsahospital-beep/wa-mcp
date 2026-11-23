"""English default word lists. Config tasks.keywords.<name> and insight.words.<name> extend them."""

from __future__ import annotations

import re
from collections.abc import Iterable

_APOSTROPHE = chr(0x2019)


def fold(text: str) -> str:
    """Casefolded text with typographic apostrophes made plain, for matching."""
    return (text or "").casefold().replace(_APOSTROPHE, "'")


def fold_map(text: str) -> tuple[str, list[int]]:
    """fold(text) and, for each folded character, the index of the original character it came from.

    Casefolding can change the length (sharp s becomes "ss"), so positions found in folded text must
    be mapped back before slicing the original.
    """
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text or ""):
        folded = fold(ch)
        out.append(folded)
        index.extend([i] * len(folded))
    return "".join(out), index


class Words:
    """A compiled word list: case-insensitive, matches at word starts, "*" suffix allows any ending.

    find() returns the list items that matched (never the message text around them).
    """

    def __init__(self, items: Iterable[str]) -> None:
        cleaned = sorted(
            {re.sub(r"\s+", " ", fold(w.strip())) for w in items if w and w.strip()}, key=len, reverse=True
        )
        self.items = tuple(cleaned)
        self._exact = {w for w in cleaned if not w.endswith("*")}
        self._stems = [w for w in cleaned if w.endswith("*")]
        parts = []
        for w in cleaned:
            stem = w[:-1] if w.endswith("*") else w
            pat = r"\s+".join(re.escape(part) for part in stem.split())
            parts.append(pat + (r"\w*" if w.endswith("*") else r"(?!\w)"))
        self._rx = re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")") if parts else None

    def _item(self, hit: str) -> str:
        if hit in self._exact:
            return hit
        return next((w for w in self._stems if hit.startswith(w[:-1])), hit)

    def spans(self, text: str) -> list[tuple[int, int, str]]:
        """(start, end, list item) for every match, positions in fold(text)."""
        if self._rx is None or not text:
            return []
        low = fold(text)
        return [
            (m.start(), m.end(), self._item(re.sub(r"\s+", " ", m.group(0)))) for m in self._rx.finditer(low)
        ]

    def find(self, text: str) -> list[str]:
        out: list[str] = []
        for _, _, item in self.spans(text):
            if item not in out:
                out.append(item)
        return out

    def any(self, text: str) -> bool:
        return bool(self._rx is not None and text and self._rx.search(fold(text)))


TASK_WORDS: dict[str, tuple[str, ...]] = {
    "request": (
        "send", "send me", "make", "fix", "add", "remove", "change", "update", "check", "review", "prepare",
        "write", "call me", "book", "order", "upload", "share", "print", "scan", "create", "build", "finish",
        "deliver", "draft", "set up", "look at", "take care of", "forward", "buy", "pay", "schedule", "translate",
        "edit", "replace", "delete", "install", "renew", "submit", "sign", "move", "rename", "post", "publish",
    ),
    "urgent": (
        "please", "pls", "plz", "asap", "urgent", "urgently", "today", "tonight", "by tomorrow", "tomorrow morning",
        "important", "quickly", "right away", "as soon as", "when you can", "when you get a chance",
    ),
    "want": ("i want", "we want", "i need", "we need", "would like", "i'd like", "we'd like"),
    "deliverable": (
        "presentation", "deck", "slides", "slide", "site", "website", "page", "link", "app", "video", "report",
        "document", "doc", "file", "design", "logo", "invoice", "contract", "email", "post", "banner", "list",
        "photo", "photos", "draft", "version", "form", "table", "spreadsheet", "offer", "quote",
    ),
    "problem": (
        "broken", "not working", "doesn't work", "does not work", "isn't working", "bug", "error", "wrong", "glitch",
        "typo", "mistake", "crash*", "fails", "failing", "missing", "can't open", "cannot open", "won't open",
        "doesn't load", "not loading", "outdated", "old version", "looks bad", "is off",
    ),
    "follow_up": (
        "any news", "any update", "any updates", "status", "how is it going", "how's it going", "is it done",
        "is it ready", "done yet", "ready yet", "where is", "did you", "have you", "when will", "still waiting",
        "reminder", "any progress", "eta", "what's up with", "how far",
    ),
    "cancel": (
        "never mind", "nevermind", "forget it", "no need", "not needed", "cancel", "leave it as is", "drop it",
        "don't bother", "skip it", "no longer needed",
    ),
    "not_cancel": ("if not, never mind", "if not never mind", "just asking", "no pressure", "otherwise no need"),
    "chat": (
        "thanks", "thank you", "great", "cool", "nice", "ok", "okay", "lol", "haha", "good night", "good morning",
        "see you", "taxi", "on my way", "lunch", "coffee", "congrats", "happy birthday",
    ),
    "spec": (
        "brief", "spec", "requirements", "changes", "slide", "checklist", "todo", "to do", "list of", "feedback",
        "comments", "prompt", "guidelines", "instructions",
    ),
    "accept": (
        "ok", "okay", "sure", "will do", "on it", "got it", "i'll", "i will", "noted", "yes", "yep", "fine", "deal",
        "looking", "checking", "will check", "will look",
    ),
    "done": (
        "done", "sent", "ready", "fixed", "finished", "uploaded", "delivered", "here it is", "here you go",
        "completed", "updated", "all set",
    ),
    "thanks": (
        "thanks", "thank you", "thx", "perfect", "great", "works now", "looks good", "all good", "awesome", "approved",
    ),
    "progress": ("working on it", "on it", "in progress", "almost done", "started", "starting", "halfway"),
    "waiting": ("waiting for", "need from you", "send me the", "can you send", "once you send", "do you have"),
}  # fmt: skip

INSIGHT_WORDS: dict[str, tuple[str, ...]] = {
    "deferral": (
        "later", "tomorrow", "in a bit", "in a while", "next week", "after lunch", "let me check", "i'll look",
        "i'll get back", "get back to you", "soon", "one sec", "give me a minute", "tonight", "not now", "busy now",
    ),
    "promise": (
        "i will", "i'll", "ill send", "will send", "will do", "will check", "will call", "will fix", "i can send",
        "let me send", "i'm going to", "gonna", "will be ready", "will get back", "you'll have it", "you will have it",
        "i'll have it",
    ),
    "formal": (
        "dear", "kind regards", "best regards", "regards", "sincerely", "please find", "would you", "could you please",
        "thank you for", "mr", "mrs", "ms", "sir", "madam", "i would appreciate", "at your convenience",
    ),
    "informal": (
        "hey", "yo", "lol", "haha", "gonna", "wanna", "bro", "dude", "u", "thx", "np", "btw", "omg", "ya", "yeah",
    ),
    "ask": ("can you", "could you", "would you", "please", "pls", "send", "need", "let me know"),
    "greeting": ("hi", "hello", "hey", "good morning", "good evening", "morning"),
    "farewell": ("bye", "good night", "see you", "talk later", "cya"),
    "laugh": ("haha*", "hehe*", "lol", "lmao", "rofl", "lmfao"),
    "negation": (
        "not", "isn't", "isnt", "wasn't", "aren't", "haven't", "hasn't", "hadn't", "didn't", "don't", "doesn't",
        "won't", "can't", "cannot", "never", "nothing", "without",
    ),
    "stopwords": (
        "the", "and", "you", "are", "for", "with", "this", "that", "will", "have", "just", "not", "but", "can",
        "was", "what", "how", "its", "it's", "i'm", "your", "our", "they", "them", "from", "all", "any", "about",
        "there", "then", "than", "when", "also", "yes", "okay", "too", "get", "got", "did", "does", "out", "now",
        "one", "who", "why", "here", "his", "her", "she", "him", "has", "had", "been", "were", "would", "could",
        "should", "into", "some", "very", "more", "much", "let", "see", "know", "think", "at", "to", "of", "in",
        "on", "is", "it", "a", "i", "me", "my", "we", "be", "do", "so", "if", "or", "as", "an", "by", "no", "up",
    ),
}  # fmt: skip
