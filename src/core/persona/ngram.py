"""Word n-gram overlap against the quote banks (anti-parroting, spec 3.12.5)."""

from __future__ import annotations

import re
from collections.abc import Iterable

_WORD = re.compile(r"[a-z0-9']+")
MIN_PHRASE_WORDS = 3


def words(text: str) -> list[str]:
    """Lower-cased word tokens (apostrophes kept, other punctuation dropped)."""
    return _WORD.findall(text.lower().replace("’", "'"))


def window_for(phrase_words: int, limit: int) -> int:
    """Window size: ``limit`` words, 5 for lines under 10 words (whole line if shorter)."""
    if phrase_words >= 10:
        return limit
    return min(5, phrase_words)


def overlapping(text: str, phrases: Iterable[tuple[str, str]], limit: int = 8) -> list[str]:
    """Return the ids of ``(id, phrase)`` pairs that ``text`` reproduces beyond the window."""
    reply = words(text)
    grams: dict[int, set[tuple[str, ...]]] = {}
    hits: list[str] = []
    for ident, phrase in phrases:
        pw = words(phrase)
        if len(pw) < MIN_PHRASE_WORDS:
            continue
        size = window_for(len(pw), limit)
        if size not in grams:
            grams[size] = {tuple(reply[i : i + size]) for i in range(len(reply) - size + 1)}
        reply_grams = grams[size]
        if any(tuple(pw[i : i + size]) in reply_grams for i in range(len(pw) - size + 1)):
            hits.append(ident)
    return hits
