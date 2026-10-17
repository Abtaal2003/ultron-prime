"""Capabilities: where a value came from and who may read it (spec 4.11).

Every value the interpreter handles is a :class:`Value` carrying a :class:`Capability`. A
capability lists the *sources* the value was derived from and the *readers* allowed to see it.
Combining values unions their sources and intersects their readers, so a result can never be
more trusted, or more public, than its inputs. Policies read these tags before a tool runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OPERATOR = "operator"
CONSTANT = "constant"
TRUSTED_KINDS = frozenset({OPERATOR, CONSTANT})
"""Source kinds that count as the operator's own words (a literal in the plan came from them)."""
PRIVATE_KINDS = frozenset({"file", "upload", "memory", "history"})
OPERATOR_ONLY: frozenset[str] = frozenset({OPERATOR})
"""The readers of private data: the operator and nobody else."""


@dataclass(frozen=True, slots=True, order=True)
class Source:
    """One origin of a value, such as ``operator``, ``web:call_3`` or ``memory:mem_9``."""

    kind: str
    ref: str = ""

    def __str__(self) -> str:
        return f"{self.kind}:{self.ref}" if self.ref else self.kind


@dataclass(frozen=True, slots=True)
class Capability:
    """Sources and readers of a value. ``readers=None`` means public."""

    sources: frozenset[Source]
    readers: frozenset[str] | None = None

    @property
    def trusted(self) -> bool:
        """True only when every source is the operator or a constant from the plan."""
        return all(s.kind in TRUSTED_KINDS for s in self.sources)

    @property
    def private(self) -> bool:
        """True when only some principals may read the value (it must not leave the machine)."""
        return self.readers is not None

    @property
    def has_operator_evidence(self) -> bool:
        """True when at least one source is the operator's own words."""
        return any(s.kind in TRUSTED_KINDS for s in self.sources)

    def union(self, *others: Capability) -> Capability:
        """Combine capabilities: sources are unioned and readers intersected."""
        sources = self.sources.union(*(o.sources for o in others))
        readers = self.readers
        for other in others:
            if other.readers is None:
                continue
            readers = other.readers if readers is None else readers & other.readers
        if sources == self.sources and readers == self.readers:
            return self
        return Capability(sources, readers)

    def describe(self) -> list[str]:
        """Sorted source labels, for the confirmation card."""
        return sorted(str(s) for s in self.sources)


EMPTY_CAP = Capability(frozenset())
"""No dependencies at all: the starting point for control-flow tracking."""
OPERATOR_CAP = Capability(frozenset({Source(OPERATOR)}))
CONSTANT_CAP = Capability(frozenset({Source(CONSTANT)}))

# Where each tool's output comes from. Files, uploads, memories and history are private to the
# operator; web content is untrusted but public; everything else is untrusted tool output.
TOOL_SOURCES: dict[str, tuple[str, bool]] = {
    "web_omniscience_search": ("web", False),
    "attachment_reader": ("upload", True),
    "file_system_scribe": ("file", True),
    "recall_memories": ("memory", True),
    "search_past_chats": ("history", True),
    "recall_chat": ("history", True),
    "artifact_forge": ("file", True),
}


def tool_capability(tool_name: str, call_id: str) -> Capability:
    """The capability of whatever ``tool_name`` returned."""
    kind, private = TOOL_SOURCES.get(tool_name, ("tool", False))
    return Capability(
        frozenset({Source(kind, call_id or tool_name)}),
        OPERATOR_ONLY if private else None,
    )


@dataclass(slots=True)
class Value:
    """A Python value with its capability."""

    data: Any
    cap: Capability

    def tainted_by(self, extra: Capability) -> Value:
        """The same data, carrying ``extra`` as well."""
        merged = self.cap.union(extra)
        return self if merged is self.cap else Value(self.data, merged)


def is_pure_constant(cap: Capability) -> bool:
    """True for a capability made only of plan literals (they add structure, not content)."""
    return bool(cap.sources) and all(s.kind == CONSTANT for s in cap.sources)


def content_caps(*caps: Capability) -> list[Capability]:
    """Drop pure-literal capabilities so an index or separator never counts as evidence."""
    return [c for c in caps if not is_pure_constant(c)]


def combine(*values: Value) -> Capability:
    """Union of every value's capability (``CONSTANT_CAP`` for none)."""
    if not values:
        return CONSTANT_CAP
    first, rest = values[0].cap, [v.cap for v in values[1:]]
    return first.union(*rest)
