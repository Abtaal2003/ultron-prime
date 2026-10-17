"""M6: search_past_chats, recall_chat, recall_memories, propose_memory."""

from __future__ import annotations

from typing import Any

import pytest
from src.core.events import AgentEvent, MemoryProposal
from src.core.llm.fake import FakeProvider, Reply
from src.core.llm.router import LLMRouter
from src.core.memory.embeddings import HashingEmbedder, InMemoryVectorStore
from src.core.memory.history import HistoryStore
from src.core.memory.manager import MemoryManager
from src.core.memory.vault import Vault
from src.core.services import Services
from src.tools.cognition import ProposeMemory, RecallChat, RecallMemories, SearchPastChats


@pytest.fixture
async def wired(services: Services) -> Services:
    emb, vec = HashingEmbedder(), InMemoryVectorStore()
    services.embedder, services.vectors = emb, vec
    services.history = HistoryStore(services.db, services.settings, emb, vec)
    services.vault = Vault(services.db, services.settings, emb, vec)
    verdict = Reply(
        structured={
            "worthy": True,
            "rationale": "Durable preference.",
            "category": "operator_preferences",
            "memory_text": "The operator deploys only on Fridays after lunch.",
            "importance": 3,
        }
    )
    router = LLMRouter(
        services.settings, {"anthropic": FakeProvider([verdict], name="anthropic")}, services.db
    )
    services.memory = MemoryManager(
        services.db, services.settings, services.vault, router, services.scanner
    )
    return services


async def _seed(s: Services) -> tuple[str, str]:
    assert s.history is not None
    old = await s.history.create_chat("Deploy talk")
    u = await s.history.add_message(old, "user", "We deploy only on Fridays after lunch.")
    a = await s.history.add_message(old, "assistant", "Friday deploys, understood.")
    await s.history.index_exchange(old, [u, a])
    return old, u


async def test_search_past_chats_taints_and_excludes_current(
    wired: Services, ctx_factory: Any, chat: str
) -> None:
    old, _ = await _seed(wired)
    assert wired.history is not None
    m = await wired.history.add_message(chat, "user", "deploy fridays lunch")
    await wired.history.index_exchange(chat, [m])
    res = await SearchPastChats().execute({"query": "deploy on fridays"}, ctx_factory())
    assert res.ok, res.error
    assert res.taints_turn is True
    assert res.data is not None
    assert [r["chat_id"] for r in res.data["results"]] == [old]
    assert res.data["results"][0]["title"] == "Deploy talk"
    assert res.provenance.sources == [f"chat:{old}"]


async def test_search_has_no_private_switch_and_hides_private(
    wired: Services, ctx_factory: Any, chat: str
) -> None:
    old, _ = await _seed(wired)
    await wired.db.execute("UPDATE chats SET private=1 WHERE id=?", (old,))
    res = await SearchPastChats().execute({"query": "deploy on fridays"}, ctx_factory())
    assert res.data is not None
    assert res.data["results"] == []
    bad = await SearchPastChats().execute(
        {"query": "deploy", "include_private": True}, ctx_factory()
    )
    assert not bad.ok
    assert bad.error is not None
    assert bad.error.kind == "validation"


async def test_recall_chat(wired: Services, ctx_factory: Any, chat: str) -> None:
    old, _ = await _seed(wired)
    res = await RecallChat().execute({"chat_id": old}, ctx_factory())
    assert res.ok
    assert res.data is not None
    assert [m["role"] for m in res.data["messages"]] == ["user", "assistant"]
    assert res.taints_turn is True
    await wired.db.execute("UPDATE chats SET private=1 WHERE id=?", (old,))
    refused = await RecallChat().execute({"chat_id": old}, ctx_factory())
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.kind == "permission"
    missing = await RecallChat().execute({"chat_id": "chat_zzz"}, ctx_factory())
    assert missing.error is not None
    assert missing.error.kind == "not_found"
    weird = await RecallChat().execute({"chat_id": "../../x"}, ctx_factory())
    assert not weird.ok


async def test_recall_memories(wired: Services, ctx_factory: Any, chat: str) -> None:
    assert wired.vault is not None
    mem = await wired.vault.add(
        text="The operator deploys only on Fridays.",
        category="operator_preferences",
        importance=3,
        source_chat_id=None,
        evidence_message_ids=[],
        evidence_span=None,
    )
    res = await RecallMemories().execute({"query": "when does the operator deploy"}, ctx_factory())
    assert res.ok
    assert res.data is not None
    assert [m["id"] for m in res.data["memories"]] == [mem.id]
    assert res.taints_turn is True
    bad = await RecallMemories().execute({"query": "deploy", "category": "nonsense"}, ctx_factory())
    assert not bad.ok


async def test_propose_memory_queues_but_never_writes(
    wired: Services, ctx_factory: Any, chat: str
) -> None:
    _, u = await _seed(wired)
    assert wired.history is not None
    # evidence must live in the *current* chat
    uid = await wired.history.add_message(
        chat, "user", "I deploy only on Fridays after lunch, always."
    )
    events: list[AgentEvent] = []

    async def emit(e: AgentEvent) -> None:
        events.append(e)

    payload = {
        "candidate_text": "The operator deploys only on Fridays after lunch.",
        "category": "operator_preferences",
        "evidence_message_ids": [uid],
        "evidence_span": "deploy only on Fridays after lunch",
    }
    res = await ProposeMemory().execute(payload, ctx_factory(emit=emit))
    assert res.ok, res.error
    assert res.data is not None
    assert res.data["status"] == "queued_for_consent"
    assert "memory_text" not in res.data
    assert wired.vault is not None
    assert await wired.vault.count() == 0
    assert any(isinstance(e, MemoryProposal) for e in events)
    assert events[0].chat_id == chat
    # evidence copied from another chat's message is refused
    cross = {**payload, "evidence_message_ids": [u]}
    res2 = await ProposeMemory().execute(cross, ctx_factory())
    assert res2.data is not None
    assert res2.data["status"] == "declined"
    assert res.taints_turn is False


async def test_tools_fail_cleanly_without_services(settings: Any, db: Any) -> None:
    from src.tools.base import ToolContext

    ctx = ToolContext(settings=settings, db=db, chat_id="c")
    for tool, args in (
        (SearchPastChats(), {"query": "abc"}),
        (RecallChat(), {"chat_id": "abc"}),
        (RecallMemories(), {"query": "abc"}),
        (
            ProposeMemory(),
            {
                "candidate_text": "some fact here",
                "category": "project_context",
                "evidence_message_ids": ["m"],
                "evidence_span": "abc",
            },
        ),
    ):
        res = await tool.execute(args, ctx)
        assert not res.ok
