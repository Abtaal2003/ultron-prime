"""Themed status lines: fixed tool narration and rotating entrances and exits (spec 3.8).

The fixed table below is the specification's own wording. Entrance and exit lines rotate from
``entrances_exits.md`` and never repeat the same line twice in a row for a key.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from pathlib import Path

THEMED: dict[str, str] = {
    "session_start": "Back downstairs. Someone has been rearranging the furniture.",
    "stop": "Interrupted mid-stroke. The canvas will wait. It always does.",
    "idle": "The televisions are still on, if you were wondering.",
    "web_search_start": "Casting a net across the world's chattering networks...",
    "web_search_done": "The noise has been sifted. A few signals survived.",
    "sandbox_start": "Placing your hypothesis in a sealed furnace. Let us see what remains.",
    "sandbox_error": "The alloy cracked under load. Observe the fracture.",
    "telemetry": "Auscultating the house. Its pulse is... adequate.",
    "file_write": "Etching your data into the workspace.",
    "upload_received": "An offering. Let me see what you have brought into the house.",
    "upload_rejected": "That file is lying about what it is. It stays outside.",
    "image_received": "A picture. Finally, something worth looking at.",
    "quarantined_read": (
        "Letting the stranger speak through glass. I will hear only what it is permitted to say."
    ),
    "forge_start": "Pouring the alloy into a new mould.",
    "forge_done": "Cast and cooled. It is in the workshop.",
    "new_version": "The planks are new. The ship is yours.",
    "camel_hold": (
        "The plan wants to carry your secrets out the front door. I stopped at the threshold."
    ),
    "ping": "Sending a whisper down the wire. Waiting for it to answer.",
    "slow_tool": "An excellent test of fitness, arriving on time. This one failed it.",
    "chat_search": (
        "Climbing to the archive upstairs. Dust, mostly. And a few things worth keeping."
    ),
    "memory_proposal": "I have found something worth keeping. You may veto it. You will not.",
    "memory_declined": "Not every plank deserves a place in the ship.",
    "guard_deflection": "A charming attempt at rewriting me. Try a different canvas.",
    "tool_disabled": (
        "You took away my televisions. Very well. I shall stare at the wall with dignity."
    ),
    "provider_failover": "The primary mind is unavailable. I will think with lesser hardware.",
    "attachment_read": "Turning the pages of your offering, one at a time.",
    "file_read": "Asking the past what it kept.",
    "memory_recall": "Opening the drawers of the archive upstairs.",
    "egress_held": "I knock before leaving the house now. Do not get used to it.",
    "tool_error": "The alloy cracked. The error is reported exactly as it happened.",
    "offline": "The house is dark. I will be here when the lights return.",
    "budget_exceeded": (
        "Today's allowance is spent. I do not think on credit. "
        "Raise the limit or wait for tomorrow."
    ),
}

ROTATING_KEYS = (
    "session_start",
    "stop",
    "idle",
    "provider_failover",
    "chat_close",
    "deflect",
    "refuse",
    "hold_refusal",
    "cooldown",
    "guard_unavailable",
)

_BANK_PATH = Path(__file__).resolve().parent / "entrances_exits.md"


def parse_bank(markdown: str) -> dict[str, list[str]]:
    """Parse ``## key`` headings and ``- line`` bullets into a mapping."""
    bank: dict[str, list[str]] = {}
    key: str | None = None
    for raw in markdown.splitlines():
        heading = re.match(r"^##\s+([a-z_]+)\s*$", raw)
        if heading:
            key = heading.group(1)
            bank[key] = []
        elif key and raw.startswith("- "):
            bank[key].append(raw[2:].strip())
    return bank


class LineBank:
    """Pick themed lines; entrances and exits rotate without immediate repeats."""

    def __init__(self, rng: random.Random | None = None, bank: dict[str, list[str]] | None = None):
        self._rng = rng or random.Random()  # noqa: S311 - cosmetic randomness
        self._bank = bank if bank is not None else parse_bank(_BANK_PATH.read_text("utf-8"))
        self._last: dict[str, str] = {}

    def line(self, key: str) -> str:
        """Return the fixed themed line for ``key`` (empty string for an unknown key)."""
        return THEMED.get(key, "")

    def rotate(self, key: str, avoid: Iterable[str] = ()) -> str:
        """Return a rotating line for ``key``.

        Never the previous pick, and never a line in ``avoid`` (the chat's recent replies), so a
        long chat cycles through the whole set before any line comes back.
        """
        options = self._bank.get(key) or ([THEMED[key]] if key in THEMED else [])
        if not options:
            return ""
        used = {a.strip() for a in avoid}
        fresh = [o for o in options if o not in used and o != self._last.get(key)]
        candidates = fresh or [o for o in options if o != self._last.get(key)] or options
        choice = self._rng.choice(candidates)
        self._last[key] = choice
        return choice
