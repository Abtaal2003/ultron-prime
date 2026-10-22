"""Compose the whole system from settings (spec 12.3: dependency injection throughout).

``build_runtime`` is the single place that knows how the pieces fit. The API, the CLI, and the
tests all call it; tests pass fake providers, an in-memory checkpointer, and a hashing embedder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from src.config import Settings
from src.core import tracing
from src.core.agent import GuardSuite, UltronAgent
from src.core.db import Database, SqliteTurnEventSink
from src.core.events import EventBus
from src.core.guards.drift import DriftMonitor
from src.core.guards.egress_policy import TaintAwareEgressPolicy
from src.core.guards.output import OutputGuard
from src.core.guards.quarantine import QuarantinedReader
from src.core.guards.repeat import COLLECTION as REPEAT_COLLECTION
from src.core.guards.repeat import RepeatDetector
from src.core.guards.scope import ScopeGuard
from src.core.llm.base import LLMProvider
from src.core.llm.router import LLMRouter
from src.core.memory import history as history_mod
from src.core.memory import vault as vault_mod
from src.core.memory.embeddings import (
    Embedder,
    VectorStore,
    build_embedder,
    build_vector_store,
    ensure_collection,
)
from src.core.memory.history import HistoryStore
from src.core.memory.manager import MemoryManager
from src.core.memory.vault import Vault
from src.core.memory.window import WindowBuilder
from src.core.persona.anchors import AnchorIndex
from src.core.persona.loader import PersonaPack, load_persona
from src.core.services import Services
from src.files.ingest import FileService
from src.files.store import FileStore
from src.logging_setup import ensure_safe_logging, get_logger
from src.tools.catalog import build_registry
from src.tools.registry import ToolRegistry

log = get_logger("wiring")


def build_providers(settings: Settings) -> dict[str, LLMProvider]:
    """Create an adapter for every provider that has an API key."""
    providers: dict[str, LLMProvider] = {}
    if settings.anthropic_api_key.get_secret_value():
        from src.core.llm.anthropic_provider import AnthropicProvider

        providers["anthropic"] = AnthropicProvider(settings.anthropic_api_key.get_secret_value())
    if settings.groq_api_key.get_secret_value():
        from src.core.llm.groq_provider import GroqProvider

        providers["groq"] = GroqProvider(settings.groq_api_key.get_secret_value())
    if settings.gemini_api_key.get_secret_value():
        from src.core.llm.gemini_provider import GeminiProvider

        providers["gemini"] = GeminiProvider(settings.gemini_api_key.get_secret_value())
    return providers


@dataclass(slots=True)
class Runtime:
    """Everything a running application owns."""

    settings: Settings
    db: Database
    services: Services
    registry: ToolRegistry
    agent: UltronAgent
    router: LLMRouter
    bus: EventBus
    pack: PersonaPack
    files: FileService
    guards: GuardSuite
    _closers: list[Any] = field(default_factory=list)

    async def close(self) -> None:
        """Stop turns, close the checkpointer connection, and close the database."""
        await self.agent.shutdown()
        for closer in self._closers:
            await closer()
        await self.db.close()
        tracing.shutdown_tracing()


async def build_runtime(
    settings: Settings,
    *,
    providers: dict[str, LLMProvider] | None = None,
    embedder: Embedder | None = None,
    vectors: VectorStore | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    registry: ToolRegistry | None = None,
) -> Runtime:
    """Create and connect every component."""
    ensure_safe_logging(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    if settings.otel_enabled and not tracing.enabled():
        tracing.setup_tracing(settings)
    db = Database(settings.sqlite_file)
    await db.connect()
    closers: list[Any] = []
    if checkpointer is None:
        conn = await aiosqlite.connect(settings.checkpoint_file)
        checkpointer = AsyncSqliteSaver(conn)
        closers.append(conn.close)
    emb = embedder or build_embedder(settings)
    vec = vectors or build_vector_store(settings)
    bus = EventBus(SqliteTurnEventSink(db))
    router = LLMRouter(
        settings, providers if providers is not None else build_providers(settings), db
    )
    store = FileStore(db, settings)
    services = Services(
        settings=settings, db=db, files=store, bus=bus, embedder=emb, vectors=vec, router=router
    )
    history = HistoryStore(db, settings, emb, vec)
    vault = Vault(db, settings, emb, vec)
    services.history, services.vault = history, vault
    services.memory = MemoryManager(db, settings, vault, router, services.scanner)
    services.quarantine = QuarantinedReader(router, settings)
    pack = load_persona(settings)
    repeat = RepeatDetector(db, emb, vec, settings)
    await ensure_collection(db, vec, emb, history_mod.COLLECTION, history.rebuild_index)
    await ensure_collection(db, vec, emb, vault_mod.COLLECTION, vault.rebuild_index)
    await ensure_collection(db, vec, emb, REPEAT_COLLECTION, repeat.rebuild_index)
    drift = DriftMonitor(db, emb, pack.contrast_pairs)
    guards = GuardSuite(
        scope=ScopeGuard(router, settings),
        output=OutputGuard(settings, services.scanner, router),
        window=WindowBuilder(history, router, settings),
        repeat=repeat if settings.repeat_detection else None,
        drift=drift,
        anchors=AnchorIndex(emb),
    )
    reg = registry or build_registry()
    agent = UltronAgent(
        settings,
        services,
        reg,
        guards,
        TaintAwareEgressPolicy(settings.egress_policy),
        pack,
        checkpointer,
    )
    return Runtime(
        settings=settings,
        db=db,
        services=services,
        registry=reg,
        agent=agent,
        router=router,
        bus=bus,
        pack=pack,
        files=FileService(store, settings, services.scanner),
        guards=guards,
        _closers=closers,
    )
