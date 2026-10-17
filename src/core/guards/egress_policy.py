"""Taint-aware egress policy (spec 4.9) behind the ``FlowPolicy`` interface.

Ultron holds private data (memories, history, files), reads untrusted content, and can reach the
outside world (a search query or a hostname leaves the machine). Once a turn has read untrusted
content, an egress call may only proceed if its outbound arguments are *traceable to the
operator's own message*; otherwise it is **held** for the operator's decision, or denied.

``FlowPolicy`` is the seam the CaMeL capability policies (spec 4.11) later implement.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Protocol

Action = Literal["allow", "hold", "deny"]

_TOKEN = re.compile(r"[a-z0-9][a-z0-9._@:/\-]*")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
        "about",
        "can",
        "could",
        "do",
        "does",
        "find",
        "get",
        "give",
        "latest",
        "look",
        "new",
        "search",
        "show",
        "tell",
        "up",
        "us",
    ]
)
COVERAGE_THRESHOLD = 0.8
ENCODED_TOKEN_LEN = 16
MAX_UNTRACED_TOKENS = 2
"""Most tokens an outbound string may add beyond the operator's words (bounds padding attacks)."""
_YEAR = re.compile(r"(19|20)\d\d")
_DIGITS = re.compile(r"\d")


def normalise(text: str) -> str:
    """NFKC + casefold + whitespace collapse (the form used for traceability checks)."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    """Tokenise normalised text into content-bearing tokens (stop words removed)."""
    return [t.strip(".:/-@_") for t in _TOKEN.findall(normalise(text)) if t not in _STOPWORDS]


def traceable(outbound: str, operator_text: str) -> bool:
    """Return True when ``outbound`` is derivable from what the operator typed.

    A normalised substring match passes. Otherwise at least 80 percent of the outbound's content
    tokens must appear in the operator's message, and the tokens it adds must stay few and
    harmless: no long token (a typical carrier for encoded data), no more than
    ``MAX_UNTRACED_TOKENS`` of them (so padding with the operator's own words cannot smuggle a
    secret out in pieces), and none that look like codes (three or more digits, years excepted).
    """
    out_norm, op_norm = normalise(outbound), normalise(operator_text)
    if not out_norm:
        return True
    if out_norm in op_norm:
        return True
    out_tokens = [t for t in tokens(outbound) if t]
    if not out_tokens:
        return True
    op_tokens = set(tokens(operator_text))
    extra = [t for t in out_tokens if t not in op_tokens]
    if any(len(t) >= ENCODED_TOKEN_LEN for t in extra):
        return False
    if len(extra) > MAX_UNTRACED_TOKENS:
        return False
    if any(len(_DIGITS.findall(t)) >= 3 and not _YEAR.fullmatch(t) for t in extra):
        return False
    return (len(out_tokens) - len(extra)) / len(out_tokens) >= COVERAGE_THRESHOLD


@dataclass(slots=True)
class FlowRequest:
    """Everything a flow policy needs to judge one call."""

    tool_name: str
    is_egress: bool
    outbound: list[tuple[str, str]]
    operator_text: str
    tainted: bool
    holds_so_far: int = 0
    max_holds: int = 2
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class FlowDecision:
    """The policy's verdict."""

    action: Action
    reason: str = ""
    policy: str = "taint_egress"
    outbound_string: str = ""


class FlowPolicy(Protocol):
    """Decides whether a tool call may proceed given what the turn has read."""

    def evaluate(self, request: FlowRequest) -> FlowDecision:
        """Return allow, hold (ask the operator), or deny."""
        ...


def render_outbound(outbound: list[tuple[str, str]], limit: int = 600) -> str:
    """Render outbound arguments exactly as they would leave the machine (capped for the UI)."""
    text = "\n".join(f"{name}: {value}" for name, value in outbound)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TaintAwareEgressPolicy:
    """``EGRESS_POLICY`` = ``confirm_tainted`` | ``deny_tainted`` | ``off``."""

    def __init__(self, mode: Literal["confirm_tainted", "deny_tainted", "off"]) -> None:
        self.mode = mode

    def evaluate(self, request: FlowRequest) -> FlowDecision:
        """Apply the taint rules from spec 4.9."""
        if self.mode == "off" or not request.is_egress:
            return FlowDecision("allow", "no egress policy applies")
        if not request.tainted:
            return FlowDecision("allow", "turn has not read untrusted content")
        untraceable = [
            (name, value)
            for name, value in request.outbound
            if not traceable(value, request.operator_text)
        ]
        if not untraceable:
            return FlowDecision("allow", "outbound arguments come from the operator's message")
        shown = render_outbound(request.outbound)
        reason = "this turn read untrusted content, and the request did not come from your words"
        if self.mode == "deny_tainted":
            return FlowDecision("deny", reason, outbound_string=shown)
        if request.holds_so_far >= request.max_holds:
            return FlowDecision(
                "deny", "too many held egress calls this turn", outbound_string=shown
            )
        return FlowDecision("hold", reason, outbound_string=shown)
