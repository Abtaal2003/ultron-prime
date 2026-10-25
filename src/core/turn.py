"""Per-turn runtime state and the guarded sentence streamer (spec 6.3, 6.4, 12.2).

The LangGraph state only carries small, serialisable routing values. Everything mutable and
non-serialisable (the value store, gate counters, collected evidence for the output guard) lives
here, keyed by ``turn_id`` inside the agent.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.core.guards.output import OutputContext, OutputGuard
from src.core.guards.prefilter import PrefilterResult
from src.core.guards.quarantine import ValueStore
from src.core.guards.repeat import RepeatBehaviour, RepeatCandidate
from src.core.guards.scope import ScopeDecision
from src.core.llm.base import Message, ToolUsePart, Usage
from src.core.memory.window import Window
from src.core.persona import punctuation
from src.core.prompts import PromptInputs
from src.tools.base import ToolResult
from src.tools.gate import GateDecision, TurnGateState

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")


@dataclass(slots=True)
class PendingCall:
    """A tool call the model requested."""

    call: ToolUsePart
    decision: GateDecision | None = None


@dataclass(slots=True)
class Outcome:
    """The result of one tool call (or its denial)."""

    call: ToolUsePart
    result: ToolResult | None
    denied: str | None = None
    quarantine_text: str | None = None


@dataclass(slots=True)
class Turn:
    """Everything one running turn owns."""

    chat_id: str
    turn_id: str
    user_text: str
    attachment_ids: list[str]
    is_remote: bool
    user_message_id: str
    started: float = field(default_factory=time.perf_counter)
    gate: TurnGateState = field(default_factory=TurnGateState)
    store: ValueStore = field(default_factory=ValueStore)
    prefilter: PrefilterResult | None = None
    scope: ScopeDecision | None = None
    candidate: RepeatCandidate | None = None
    repeat: RepeatBehaviour | None = None
    cluster_id: str | None = None
    repeat_vec: list[float] = field(default_factory=list)
    prompt_inputs: PromptInputs = field(default_factory=PromptInputs)
    window: Window | None = None
    user_message: Message | None = None
    loop_messages: list[Message] = field(default_factory=list)
    reply_parts: list[str] = field(default_factory=list)
    receipts: list[str] = field(default_factory=list)
    tool_meta: list[dict[str, Any]] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    readable_files: set[str] = field(default_factory=set)
    known_files: set[str] = field(default_factory=set)
    known_urls: set[str] = field(default_factory=set)
    retrieved_texts: list[str] = field(default_factory=list)
    tool_names: set[str] = field(default_factory=set)
    tool_numbers: list[float] = field(default_factory=list)
    retrieval_happened: bool = False
    injection_suspected: bool = False
    pending: list[PendingCall] = field(default_factory=list)
    held: list[PendingCall] = field(default_factory=list)
    ready: list[PendingCall] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    early_decisions: dict[str, str] = field(default_factory=dict)
    decision_future: asyncio.Future[dict[str, str]] | None = None
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    effort: str | None = None
    final_text: str = ""
    status: str = "ok"
    output_status: str = "pass"
    output_issues: list[str] = field(default_factory=list)
    callback_used: str | None = None
    stream_blocked: bool = False
    camel_done: bool = False
    compacted: bool = False
    size_retried: bool = False
    schema_retries: int = 0
    memory_statuses: list[str] = field(default_factory=list)
    files_named: list[str] = field(default_factory=list)
    private: bool = False
    recent_replies: list[str] = field(default_factory=list)
    tool_texts: list[str] = field(default_factory=list)
    offered_tools: set[str] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    @property
    def streamed_text(self) -> str:
        """Everything emitted to the reply so far."""
        return "".join(self.reply_parts)


class SentenceStreamer:
    """Releases text sentence by sentence after cheap checks (``STREAM_MODE=guarded``).

    A sentence is withheld (and streaming stops) when the cumulative text trips the canary or
    charter check; secrets are redacted before release. The full output guard still runs on the
    complete reply and may replace everything with ``AgentDone.final_text``.
    """

    def __init__(self, guard: OutputGuard, ctx: OutputContext, *, raw: bool) -> None:
        self.guard = guard
        self.ctx = ctx
        self.raw = raw
        self._buffer = ""
        self._released = ""
        self.blocked = False

    def _vet(self, sentence: str) -> str | None:
        if self.blocked:
            return None
        candidate = self._released + sentence
        issues: list[Any] = []
        self.guard.check_leaks(candidate + self._buffer, self.ctx, issues)
        if issues:
            self.blocked = True
            return None
        if self.guard.scanner.contains_secret(sentence):
            sentence = self.guard.scanner.redact(sentence)
        sentence = punctuation.tidy(sentence, in_code=self._released.count("```") % 2 == 1)
        self._released += sentence
        return sentence

    def feed(self, delta: str) -> list[str]:
        """Add streamed text; return the pieces that may be shown now."""
        if self.raw:
            return [delta] if delta else []
        self._buffer += delta
        out: list[str] = []
        while True:
            match = _SENTENCE_END.search(self._buffer)
            if not match:
                break
            end = match.end()
            piece, self._buffer = self._buffer[:end], self._buffer[end:]
            vetted = self._vet(piece)
            if vetted:
                out.append(vetted)
        return out

    def flush(self) -> list[str]:
        """Release the remainder at the end of a model call."""
        if self.raw or not self._buffer:
            return []
        piece, self._buffer = self._buffer, ""
        vetted = self._vet(piece)
        return [vetted] if vetted else []
