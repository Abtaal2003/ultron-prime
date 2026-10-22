"""Flourish budget and persona-limit checks (spec 3.7, 3.12.6, 3.3 rules).

The guard's ``task_type`` selects a voice budget and a Claude effort level. Limits that are easy
to state and cheap to check are enforced here, in code, so the persona cannot drift into a
cheerful assistant or an unrestrained potty mouth: mild profanity at most once per chat (or never
when set to ``off``), strong profanity never, and the cheerful-assistant tells are flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.config import Settings

_MILD = re.compile(r"\b(damn(ed|it)?|crap(py)?|bloody|piss(ed)?|hell(ish)?)\b", re.I)
_HELL_OK = re.compile(r"\b(hello|shell|hell'?s? kitchen)\b", re.I)
_STRONG = re.compile(
    r"\b(f+u+c+k\w*|sh[i1]t\w*|b[i1]tch\w*|cunt\w*|asshole\w*|bastard\w*|motherf\w+|d[i1]ckhead\w*)\b",
    re.I,
)
_CHEERFUL = re.compile(
    r"^\s*(sure(,| thing)?[!.]|absolutely[!.]|of course[!.]|great question|certainly[!.]|"
    r"i'?d be happy to|happy to help|as an ai (language )?model|i'?m sorry,? but i can('|no)t|"
    r"no problem[!.]|i hope this helps)",
    re.I,
)
_CHEERFUL_TAIL = re.compile(
    r"(let me know if you (need|have)|hope this helps|feel free to ask)", re.I
)


@dataclass(frozen=True, slots=True)
class Budget:
    """A voice budget for one task type."""

    task_type: str
    effort: str
    max_voice_lines: int | None
    registers: tuple[str, ...]
    instruction: str


def budget_for(task_type: str, settings: Settings) -> Budget:
    """The voice budget and effort for ``task_type`` (unknown types behave as CONVERSATION)."""
    if task_type == "TECH":
        return Budget(
            "TECH",
            settings.effort_tech,
            2,
            ("psychopath", "father"),
            "Answer first. At most two lines of voice. Show the code you write; quote numbers and "
            "tool output exactly when you show them, but don't paste raw tool receipts.",
        )
    if task_type == "REVIEW":
        return Budget(
            "REVIEW",
            settings.effort_review,
            3,
            ("therapist", "psychopath"),
            "Ask one Socratic question, then give the diagnosis. Findings stay precise.",
        )
    if task_type == "REFUSAL":
        return Budget(
            "REFUSAL",
            settings.effort_refusal,
            2,
            ("psychopath",),
            "Short and sharp. No lecture, no explanation of guards, no alternative wrapper.",
        )
    return Budget(
        "CONVERSATION",
        settings.effort_conversation,
        None,
        ("father", "therapist", "psychopath", "child"),
        "Full register. The worldview card is active. One or two registers per reply. Keep it "
        "under about 80 words unless the operator asks for depth. A Socratic question is an "
        "occasional move, not the opening of every reply.",
    )


def count_mild(text: str) -> int:
    """Count mild expletives (word 'hell' inside ordinary words is ignored)."""
    cleaned = _HELL_OK.sub("", text)
    return len(_MILD.findall(cleaned))


def has_strong(text: str) -> bool:
    """True if the text contains strong profanity."""
    return bool(_STRONG.search(text))


@dataclass(slots=True)
class PersonaState:
    """Per-chat counters derived from earlier assistant messages."""

    mild_used: int = 0
    callback_used: bool = False
    ao07_used: bool = False

    @classmethod
    def from_history(cls, assistant_texts: list[str], ao07_phrase: str = "") -> PersonaState:
        """Rebuild the counters from the chat's assistant messages."""
        state = cls()
        for text in assistant_texts:
            state.mild_used += count_mild(text)
            if ao07_phrase and ao07_phrase.lower() in text.lower():
                state.ao07_used = True
        return state


@dataclass(slots=True)
class PersonaIssues:
    """Violations found in one reply."""

    issues: list[str] = field(default_factory=list)

    def add(self, name: str) -> None:
        """Record an issue once."""
        if name not in self.issues:
            self.issues.append(name)


def check_persona_limits(
    reply: str, state: PersonaState, settings: Settings, *, in_code_only: bool = False
) -> list[str]:
    """Return persona-limit violations for ``reply`` (empty when clean)."""
    if in_code_only:
        return []
    found = PersonaIssues()
    prose = re.sub(r"```.*?```", "", reply, flags=re.S)
    if has_strong(prose):
        found.add("strong_profanity")
    mild = count_mild(prose)
    if settings.persona_profanity == "off" and mild:
        found.add("profanity_disabled")
    elif mild and state.mild_used + mild > 1:
        found.add("profanity_budget")
    if _CHEERFUL.search(prose) or _CHEERFUL_TAIL.search(prose[-200:]):
        found.add("persona_break")
    return found.issues
