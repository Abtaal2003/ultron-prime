"""Shared fixtures: isolated settings, in-memory database, and a deterministic embedder."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from src.config import Settings
from src.core.db import Database
from src.core.services import Services


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that write only inside ``tmp_path`` and never read the real ``.env``."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        sqlite_path=str(tmp_path / "data" / "ultron.db"),
        checkpoint_path=str(tmp_path / "data" / "checkpoints.db"),
        chroma_path=str(tmp_path / "data" / "chroma"),
        workspace_root=str(tmp_path / "workspace"),
        attachments_path=str(tmp_path / "data" / "attachments"),
        charter_path="./src/core/persona/charter.md",
        embedding_backend="hashing",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """A connected database with the schema applied."""
    database = Database(settings.sqlite_file)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def services(settings: Settings, db: Database) -> Services:
    """A minimal service container (database, file store, scanner)."""
    from src.core.services import Services
    from src.files.store import FileStore

    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    return Services(settings=settings, db=db, files=FileStore(db, settings))


@pytest.fixture
def ctx_factory(settings: Settings, db: Database, services: Services):  # type: ignore[no-untyped-def]
    """Build tool contexts bound to the test services."""
    from src.tools.base import ToolContext

    def make(**kw):  # type: ignore[no-untyped-def]
        base = {
            "settings": settings,
            "services": services,
            "db": db,
            "chat_id": "chat_t",
            "turn_id": "turn_t",
        }
        base.update(kw)
        return ToolContext(**base)

    return make


@pytest.fixture
async def chat(db: Database) -> str:
    """Insert the default test chat (``chat_t``) and return its id."""
    await db.execute(
        "INSERT OR IGNORE INTO chats(id,title,created_at,updated_at) VALUES ('chat_t','t','n','n')"
    )
    return "chat_t"


@pytest.fixture
def make_router(settings: Settings, db: Database):  # type: ignore[no-untyped-def]
    """Build ``(router, fake_provider)`` with every role routed to one scripted fake."""
    from src.core.llm.fake import FakeProvider
    from src.core.llm.router import LLMRouter

    def make(script=None, *, name: str = "fake", caps=None):  # type: ignore[no-untyped-def]
        routes = {key: f"{name}:m" for key in settings.model_fields if key.startswith("route_")}
        routed = settings.model_copy(
            update={
                **routes,
                "fallback_chain": [f"{name}:m"],
                "llm_max_retries": 0,
                "llm_backoff_base_s": 0.0,
            }
        )
        fake = FakeProvider(script, name=name, caps=caps)
        return LLMRouter(routed, {name: fake}, db), fake

    return make
