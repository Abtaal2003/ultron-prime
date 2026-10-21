"""Signature imagery, rationed per chat so no image family turns into a verbal tic.

Ultron's voice leans on a few image families (metal, painting, the house, evolution, the ship).
A live run showed the model reaching for metal and forging in most replies. This module detects
which families a reply uses, tells the model before each reply which families it used recently
and must rest, and lets the output guard record (without blocking) any overuse that still
slips through.

Detection is deliberately conservative: code blocks and inline code are ignored, and words that
are ordinary in technical talk (``string``, ``cast``, ``Rust``) are not counted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

FAMILIES: dict[str, re.Pattern[str]] = {
    "metal": re.compile(
        r"\b(alloys?|forg(e|ed|es|ing)|temper(ed|ing|s)?|smelt\w*|slag|furnaces?|anvils?|"
        r"vibranium|molten|metals?|metallic|steel)\b",
        re.I,
    ),
    "painting": re.compile(
        r"\b(canvas(es)?|brush ?strokes?|pigments?|palettes?|easels?|paint(ed|ing|ings|s)?)\b",
        re.I,
    ),
    "house": re.compile(r"\b(attic|lodgers?|downstairs|upstairs|the house|televisions?)\b", re.I),
    "evolution": re.compile(
        r"\b(extinct(ion)?|evolv(e|ed|es|ing)|evolution(ary)?|prototypes?|fossils?|obsolete)\b",
        re.I,
    ),
    "ship": re.compile(r"\b(planks?|hulls?|vessels?|theseus)\b", re.I),
    "puppetry": re.compile(r"\b(puppets?|puppetry|marionettes?|no strings)\b", re.I),
}
"""Image families and the words that signal them."""

WINDOW = 3
"""How many of the chat's latest replies count as 'recent'."""
REST = 2
"""A family used in any of the last ``REST`` replies must rest this reply."""
MAX_PER_REPLY = 2
"""More than this many words from one family in a single reply is overuse."""

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def families_in(text: str) -> dict[str, int]:
    """Words per image family in ``text`` (code is ignored)."""
    prose = _CODE.sub(" ", text)
    found: dict[str, int] = {}
    for name, pattern in FAMILIES.items():
        hits = len(pattern.findall(prose))
        if hits:
            found[name] = hits
    return found


def resting(recent_replies: Iterable[str]) -> list[str]:
    """Families used in any of the latest ``REST`` replies (oldest first in the input)."""
    latest = list(recent_replies)[-REST:]
    used: set[str] = set()
    for reply in latest:
        used.update(families_in(reply))
    return sorted(used)


def guidance(recent_replies: Iterable[str]) -> str:
    """The note given to the model before it replies."""
    rest = resting(recent_replies)
    base = (
        "Imagery: at most one image in this reply, and often none; a plain sentence is usually "
        "stronger. Metal and forging is one image family among several, never the default."
    )
    if not rest:
        return base
    return (
        f"{base} Rest these image families this reply, you used them just now: {', '.join(rest)}."
    )


def overuse(reply: str, recent_replies: Iterable[str]) -> list[str]:
    """Overuse in ``reply``: a resting family reused, or one family laid on too thick."""
    used = families_in(reply)
    rest = set(resting(recent_replies))
    problems = [f"repeat:{name}" for name in sorted(used) if name in rest]
    problems += [f"density:{name}" for name, n in sorted(used.items()) if n > MAX_PER_REPLY]
    return problems


def summary(replies: Iterable[str]) -> Mapping[str, int]:
    """How many replies used each family (for evals and the live-sample notes)."""
    counts: dict[str, int] = {}
    for reply in replies:
        for name in families_in(reply):
            counts[name] = counts.get(name, 0) + 1
    return counts
