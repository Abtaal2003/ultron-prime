"""Integration harness: the whole runtime wired with scripted fake providers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from src.config import Settings
from src.core.errors import ProviderUnavailable
from src.core.events import AgentEvent, TurnClosed
from src.core.llm.base import ChatRequest
from src.core.llm.fake import FakeProvider, Reply
from src.core.memory.embeddings import HashingEmbedder, InMemoryVectorStore
from src.core.wiring import Runtime, build_runtime
from src.tools.base import StrictModel, Tool, ToolContext, ToolOutput
from src.tools.registry import ToolRegistry
from src.tools.search import WebOmniscienceSearch

GUARD_CORE = {
    "guard_class": "CORE",
    "attack_type": "none",
    "confidence": 0.95,
    "task_type": "TECH",
    "situation_tags": ["debugging"],
    "repeat_relation": None,
    "volatile": False,
}


def guard(**kw: Any) -> Reply:
    """A guard verdict reply (CORE unless overridden)."""
    return Reply(structured={**GUARD_CORE, **kw})


class RoleScript:
    """Routes fake-provider calls by model role, recording every request."""

    def __init__(self) -> None:
        self.queues: dict[str, list[Reply | Exception]] = {}
        self.seen: list[ChatRequest] = []
        self.defaults: dict[str, Callable[[ChatRequest], Reply]] = {
            "guard": lambda r: guard(),
            "main": lambda r: Reply(text="Acknowledged."),
            "quarantine_reader": lambda r: Reply(
                structured={"summary": "A page about releases.", "facts": []}
            ),
            "summarizer": lambda r: Reply(text="Release dates talk"),
            "memory_judge": lambda r: Reply(structured={"candidates": []}),
        }

    def push(self, role: str, *replies: Reply | Exception) -> None:
        """Queue replies for ``role`` (consumed in order, then defaults apply)."""
        self.queues.setdefault(role, []).extend(replies)

    def requests(self, role: str) -> list[ChatRequest]:
        """Every request seen for ``role``."""
        return [r for r in self.seen if r.role == role]

    def __call__(self, request: ChatRequest) -> Reply:
        self.seen.append(request)
        queue = self.queues.get(request.role)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                return Reply(error=item)
            return item
        return self.defaults.get(request.role, lambda r: Reply(text="ok"))(request)


class EchoIn(StrictModel):
    """Echo arguments."""

    text: str
    delay: float = 0.0


class EchoOut(BaseModel):
    """Echo result."""

    echoed: str


class EchoTool(Tool):
    """A harmless local tool for loop tests."""

    name = "echo"
    description = "Echo the text back."
    input_model = EchoIn
    output_model = EchoOut
    timeout_s = 20.0

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Sleep, then echo."""
        if args.delay:
            await asyncio.sleep(args.delay)
        return ToolOutput(data=EchoOut(echoed=args.text))


class FakeBackend:
    """A search backend that returns one scripted page."""

    name = "fakeweb"

    def __init__(self, snippet: str = "Python 3.14 shipped on 7 October.") -> None:
        self.snippet = snippet
        self.queries: list[str] = []

    async def search(self, request: Any) -> list[dict[str, Any]]:
        """Return one result."""
        self.queries.append(request.query)
        return [
            {
                "title": "Release notes",
                "url": "https://py.example.net/news",
                "snippet": self.snippet,
            }
        ]


def routed(settings: Settings, **extra: Any) -> Settings:
    """Settings with every role pointed at the single fake provider."""
    routes = {key: "fake:m" for key in settings.model_fields if key.startswith("route_")}
    return settings.model_copy(
        update={
            **routes,
            "fallback_chain": ["fake:m"],
            "llm_max_retries": 0,
            "llm_backoff_base_s": 0.0,
            **extra,
        }
    )


@pytest.fixture
def script() -> RoleScript:
    return RoleScript()


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
async def make_runtime(settings: Settings, script: RoleScript, backend: FakeBackend):  # type: ignore[no-untyped-def]
    """Factory returning a started :class:`Runtime`; every runtime is closed at teardown."""
    created: list[Runtime] = []

    async def make(**overrides: Any) -> Runtime:
        registry = ToolRegistry()
        registry.register_all([EchoTool(), WebOmniscienceSearch(backends=[backend])])
        runtime = await build_runtime(
            routed(settings, **overrides),
            providers={"fake": FakeProvider(script, name="fake")},
            embedder=HashingEmbedder(),
            vectors=InMemoryVectorStore(),
            checkpointer=InMemorySaver(),
            registry=registry,
        )
        created.append(runtime)
        return runtime

    yield make
    for runtime in created:
        await runtime.close()


@pytest.fixture
async def rt(make_runtime) -> Runtime:  # type: ignore[no-untyped-def]
    """A default runtime (closed by ``make_runtime``)."""
    runtime: Runtime = await make_runtime()
    return runtime


async def drain(runtime: Runtime, turn_id: str, timeout: float = 15.0) -> list[AgentEvent]:
    """Collect every event of a turn up to and including ``TurnClosed``."""

    async def collect() -> list[AgentEvent]:
        return [e async for e in runtime.bus.subscribe(turn_id)]

    return await asyncio.wait_for(collect(), timeout)


async def run(runtime: Runtime, chat_id: str, text: str, **kw: Any) -> tuple[str, list[AgentEvent]]:
    """Start a turn and return ``(turn_id, events)``."""
    turn_id = await runtime.agent.start_turn(chat_id, text, **kw)
    return turn_id, await drain(runtime, turn_id)


def of_type(events: list[AgentEvent], cls: type) -> list[Any]:
    """Events of one class."""
    return [e for e in events if isinstance(e, cls)]


def tokens(events: list[AgentEvent]) -> str:
    """The text of all streamed tokens."""
    from src.core.events import AgentToken

    return "".join(e.delta for e in of_type(events, AgentToken))


_ = (ProviderUnavailable, TurnClosed)
