"""Typed events and the async event bus (spec 6.2).

Graph nodes and tools emit these events; the bus stamps each with a per-turn sequence number,
appends it to a durable log (``turn_events``), and fans it out to any connected SSE client. The
frontend and API depend only on these models, never on LangGraph types.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class BaseEvent(BaseModel):
    """Fields shared by every event."""

    # Every field is always present on the wire, so the generated TypeScript marks them required.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    turn_id: str | None = None
    chat_id: str | None = None
    seq: int = 0
    ts: str = Field(default_factory=utcnow_iso)


class UserMessage(BaseEvent):
    """The operator sent a message."""

    type: Literal["UserMessage"] = "UserMessage"
    text: str
    attachment_ids: list[str] = []


class TurnStarted(BaseEvent):
    """A turn task has been registered and is about to run."""

    type: Literal["TurnStarted"] = "TurnStarted"
    message_id: str


class AttachmentIngested(BaseEvent):
    """The ingestion pipeline finished with one uploaded file."""

    type: Literal["AttachmentIngested"] = "AttachmentIngested"
    attachment_id: str | None = None
    filename: str
    detected_type: str | None = None
    size: int = 0
    pages: int | None = None
    status: Literal["accepted", "rejected"]
    reason: str | None = None
    quarantined: bool = False
    secret_warning: bool = False
    themed_line: str | None = None


class QuarantineRead(BaseEvent):
    """Untrusted content was read by the quarantined reader."""

    type: Literal["QuarantineRead"] = "QuarantineRead"
    handle: str
    source: str
    schema_name: str
    extraction_preview: str


class FileWritten(BaseEvent):
    """A file was created or a new version was written."""

    type: Literal["FileWritten"] = "FileWritten"
    file_id: str
    version: int
    path: str
    logical_name: str
    sha256: str
    mime: str
    size: int
    derived_from: list[str] = []
    external_content: bool = False
    change_note: str | None = None


class PolicyHold(BaseEvent):
    """A CaMeL policy stopped a planned call (v1.1.0)."""

    type: Literal["PolicyHold"] = "PolicyHold"
    call_id: str
    tool_name: str
    policy: str
    argument_sources: dict[str, list[str]] = {}
    values_preview: dict[str, str] = {}


class GuardVerdict(BaseEvent):
    """The input guard classified the operator's message."""

    type: Literal["GuardVerdict"] = "GuardVerdict"
    guard_class: str
    attack_type: str | None = None
    confidence: float = 0.0
    action: str
    task_type: str = "CONVERSATION"
    themed_line: str | None = None


class RepeatDetected(BaseEvent):
    """The repeat detector found an earlier, equivalent request."""

    type: Literal["RepeatDetected"] = "RepeatDetected"
    relation: str
    ask_count: int
    refs: list[str] = []
    was_refused: bool = False
    volatile: bool = False


class MemoryRetrieved(BaseEvent):
    """Memory and history were retrieved for the turn."""

    type: Literal["MemoryRetrieved"] = "MemoryRetrieved"
    query: str
    memory_hits: list[dict[str, Any]] = []
    history_hits: list[dict[str, Any]] = []
    tainted: bool = False


class AgentToken(BaseEvent):
    """A guarded chunk of the assistant reply."""

    type: Literal["AgentToken"] = "AgentToken"
    delta: str


class ToolStart(BaseEvent):
    """A validated tool call is starting."""

    type: Literal["ToolStart"] = "ToolStart"
    call_id: str
    tool_name: str
    validated_args: dict[str, Any]
    themed_line: str | None = None
    reason_line: str | None = None


class ToolEnd(BaseEvent):
    """A tool call finished (successfully or not)."""

    type: Literal["ToolEnd"] = "ToolEnd"
    call_id: str
    tool_name: str
    status: Literal["ok", "error", "denied"]
    duration_ms: int
    receipt_id: str | None = None
    result_preview: str = ""
    themed_line: str | None = None
    quarantined: bool = False
    taints_turn: bool = False


class ToolError(BaseEvent):
    """A tool call failed."""

    type: Literal["ToolError"] = "ToolError"
    call_id: str
    tool_name: str
    error_type: str
    message: str


class EgressHold(BaseEvent):
    """An egress call is held awaiting the operator's decision."""

    type: Literal["EgressHold"] = "EgressHold"
    call_id: str
    tool_name: str
    outbound_string: str
    reason: str


class EgressDecision(BaseEvent):
    """The operator decided on an egress hold."""

    type: Literal["EgressDecision"] = "EgressDecision"
    call_id: str
    decision: Literal["approve", "reject"]


class MemoryProposal(BaseEvent):
    """Ultron proposes a memory; the operator must approve it."""

    type: Literal["MemoryProposal"] = "MemoryProposal"
    proposal_id: str
    text: str
    category: str
    rationale: str
    evidence: list[dict[str, Any]] = []
    themed_line: str | None = None


class MemoryDecision(BaseEvent):
    """The operator decided on a memory proposal."""

    type: Literal["MemoryDecision"] = "MemoryDecision"
    proposal_id: str
    decision: Literal["approve", "edit_approve", "decline"]
    final_text: str | None = None


class MemoryWritten(BaseEvent):
    """A memory was written to the Vault."""

    type: Literal["MemoryWritten"] = "MemoryWritten"
    memory_id: str


class OutputVerdict(BaseEvent):
    """The output guard finished checking the reply."""

    type: Literal["OutputVerdict"] = "OutputVerdict"
    status: Literal["pass", "regenerated", "blocked"]
    issues: list[str] = []
    regenerated: bool = False


class ProviderFailover(BaseEvent):
    """The router moved to another provider or model."""

    type: Literal["ProviderFailover"] = "ProviderFailover"
    from_route: str
    to_route: str
    reason: str
    thinking_stripped: bool = False
    themed_line: str | None = None


class AgentDone(BaseEvent):
    """The reply is final; the operator's input may be re-enabled."""

    type: Literal["AgentDone"] = "AgentDone"
    status: Literal["ok", "error", "stopped"] = "ok"
    final_text: str
    usage: dict[str, Any] = {}
    latency_ms: int = 0
    provider: str = ""
    model: str = ""
    effort: str | None = None
    tainted: bool = False
    message_id: str | None = None


class SystemNotice(BaseEvent):
    """A notice for the operator: entrances, exits, warnings, errors."""

    type: Literal["SystemNotice"] = "SystemNotice"
    level: Literal["info", "warning", "error"] = "info"
    message: str
    kind: str | None = None


class TurnClosed(BaseEvent):
    """Terminal event: no more events will follow for this turn."""

    type: Literal["TurnClosed"] = "TurnClosed"


AgentEvent = Annotated[
    UserMessage
    | TurnStarted
    | AttachmentIngested
    | QuarantineRead
    | FileWritten
    | PolicyHold
    | GuardVerdict
    | RepeatDetected
    | MemoryRetrieved
    | AgentToken
    | ToolStart
    | ToolEnd
    | ToolError
    | EgressHold
    | EgressDecision
    | MemoryProposal
    | MemoryDecision
    | MemoryWritten
    | OutputVerdict
    | ProviderFailover
    | AgentDone
    | SystemNotice
    | TurnClosed,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)

EVENT_TYPES: tuple[type[BaseEvent], ...] = (
    UserMessage,
    TurnStarted,
    AttachmentIngested,
    QuarantineRead,
    FileWritten,
    PolicyHold,
    GuardVerdict,
    RepeatDetected,
    MemoryRetrieved,
    AgentToken,
    ToolStart,
    ToolEnd,
    ToolError,
    EgressHold,
    EgressDecision,
    MemoryProposal,
    MemoryDecision,
    MemoryWritten,
    OutputVerdict,
    ProviderFailover,
    AgentDone,
    SystemNotice,
    TurnClosed,
)


def parse_event(type_name: str, payload_json: str) -> AgentEvent:
    """Rebuild an event from its stored ``type`` and JSON payload."""
    data = EVENT_ADAPTER.validate_json(payload_json)
    if data.type != type_name:  # pragma: no cover - defensive, the column and payload agree
        raise ValueError(f"stored event type mismatch: {type_name} != {data.type}")
    return data


class TurnEventSink(Protocol):
    """Durable storage for a turn's events, used for resume after reconnect."""

    async def append(self, event: AgentEvent) -> None:
        """Persist one already-sequenced event."""
        ...

    async def read_after(self, turn_id: str, after_seq: int) -> list[AgentEvent]:
        """Return every stored event with ``seq > after_seq`` in order."""
        ...

    async def last_seq(self, turn_id: str) -> int:
        """Return the highest stored sequence number (0 when none)."""
        ...


class InMemorySink:
    """A volatile sink for tests and tooling."""

    def __init__(self) -> None:
        self._events: dict[str, list[AgentEvent]] = defaultdict(list)

    async def append(self, event: AgentEvent) -> None:
        """Store the event in memory."""
        assert event.turn_id is not None
        self._events[event.turn_id].append(event)

    async def read_after(self, turn_id: str, after_seq: int) -> list[AgentEvent]:
        """Return events after ``after_seq``."""
        return [e for e in self._events.get(turn_id, []) if e.seq > after_seq]

    async def last_seq(self, turn_id: str) -> int:
        """Return the highest stored sequence for the turn."""
        events = self._events.get(turn_id, [])
        return events[-1].seq if events else 0


class EventBus:
    """Per-turn sequencing, durable logging, and fan-out to live subscribers."""

    def __init__(self, sink: TurnEventSink | None = None) -> None:
        self._sink: TurnEventSink = sink or InMemorySink()
        self._subs: dict[str, set[asyncio.Queue[AgentEvent | None]]] = defaultdict(set)
        self._seq: dict[str, int] = {}
        self._closed: set[str] = set()
        self._lock = asyncio.Lock()
        self._global_listeners: list[asyncio.Queue[AgentEvent]] = []

    async def publish(self, event: AgentEvent) -> AgentEvent:
        """Assign the next sequence number, persist, and fan out. Returns the stamped event."""
        if event.turn_id is None:
            raise ValueError("events must carry a turn_id before they reach the bus")
        async with self._lock:
            turn_id = event.turn_id
            if turn_id in self._closed:
                raise RuntimeError(f"turn {turn_id} is closed")
            if turn_id not in self._seq:
                self._seq[turn_id] = await self._sink.last_seq(turn_id)
            self._seq[turn_id] += 1
            stamped = event.model_copy(update={"seq": self._seq[turn_id]})
            await self._sink.append(stamped)
            for queue in list(self._subs.get(turn_id, ())):
                queue.put_nowait(stamped)
            for gq in self._global_listeners:
                gq.put_nowait(stamped)
        return stamped

    async def close_turn(self, turn_id: str) -> None:
        """Emit ``TurnClosed``, mark the turn finished, and release subscribers."""
        async with self._lock:
            if turn_id in self._closed:
                return
            if turn_id not in self._seq:
                self._seq[turn_id] = await self._sink.last_seq(turn_id)
            self._seq[turn_id] += 1
            final = TurnClosed(turn_id=turn_id, seq=self._seq[turn_id])
            await self._sink.append(final)
            self._closed.add(turn_id)
            for queue in list(self._subs.get(turn_id, ())):
                queue.put_nowait(final)
                queue.put_nowait(None)

    async def purge_expired(self, hours: float) -> int:
        """Drop stored events of finished turns older than ``hours`` (if the sink can)."""
        purge = getattr(self._sink, "purge_older_than", None)
        return int(await purge(hours)) if purge is not None else 0

    def is_closed(self, turn_id: str) -> bool:
        """Return whether the turn has emitted its terminal event in this process."""
        return turn_id in self._closed

    def mark_closed(self, turn_id: str) -> None:
        """Mark a turn closed without emitting (used for turns finished before a restart)."""
        self._closed.add(turn_id)

    async def subscribe(self, turn_id: str, after_seq: int = 0) -> AsyncIterator[AgentEvent]:
        """Yield every event after ``after_seq``: stored history first, then live events.

        The queue is registered before the history is read, so no event can fall into the gap
        between the replay and the live stream; duplicates are removed by sequence number.
        """
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._subs[turn_id].add(queue)
        try:
            last = after_seq
            for event in await self._sink.read_after(turn_id, after_seq):
                last = event.seq
                yield event
                if event.type == "TurnClosed":
                    return
            if turn_id in self._closed:
                while not queue.empty():
                    item = queue.get_nowait()
                    if item is not None and item.seq > last:
                        last = item.seq
                        yield item
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                if item.seq <= last:
                    continue
                last = item.seq
                yield item
                if item.type == "TurnClosed":
                    return
        finally:
            self._subs[turn_id].discard(queue)
            if not self._subs[turn_id]:
                self._subs.pop(turn_id, None)

    def listen_all(self) -> asyncio.Queue[AgentEvent]:
        """Return a queue that receives every event from every turn (tests, metrics)."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._global_listeners.append(queue)
        return queue

    def unlisten_all(self, queue: asyncio.Queue[AgentEvent]) -> None:
        """Stop feeding a queue returned by :meth:`listen_all`."""
        with contextlib.suppress(ValueError):
            self._global_listeners.remove(queue)
