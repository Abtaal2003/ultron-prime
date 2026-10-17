"""The dependency container handed to tools and the agent (spec 12.3).

Everything is injected, so tests can substitute a fake LLM, an in-memory database, and a
deterministic embedder without monkeypatching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import Settings
from src.core.db import Database
from src.files.store import FileStore
from src.tools.security import SecretScanner

if TYPE_CHECKING:
    from src.core.events import EventBus
    from src.core.guards.quarantine import QuarantinedReader
    from src.core.llm.router import LLMRouter
    from src.core.memory.embeddings import Embedder, VectorStore
    from src.core.memory.history import HistoryStore
    from src.core.memory.manager import MemoryManager
    from src.core.memory.vault import Vault


@dataclass(slots=True)
class Services:
    """Container for long-lived collaborators."""

    settings: Settings
    db: Database
    files: FileStore
    scanner: SecretScanner = field(default_factory=SecretScanner)
    bus: EventBus | None = None
    embedder: Embedder | None = None
    vectors: VectorStore | None = None
    history: HistoryStore | None = None
    vault: Vault | None = None
    memory: MemoryManager | None = None
    quarantine: QuarantinedReader | None = None
    router: LLMRouter | None = None
