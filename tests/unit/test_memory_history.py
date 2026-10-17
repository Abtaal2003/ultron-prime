"""M6: embeddings, vector stores, and chat history search."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.config import Settings
from src.core.db import Database
from src.core.errors import ConfigurationError, NotFound, PermissionFailure
from src.core.memory.embeddings import (
    ChromaVectorStore,
    HashingEmbedder,
    InMemoryVectorStore,
    VectorHit,
    build_embedder,
    cosine,
    ensure_collection,
    mmr,
    normalise,
)
from src.core.memory.history import (
    HistoryStore,
    estimate_tokens,
    fts_query,
    split_exchange,
)


# ---------------------------------------------------------------- embedders ---------------------
async def test_hashing_embedder_is_deterministic_and_lexical() -> None:
    emb = HashingEmbedder()
    a1, a2, b = await emb.embed(
        ["the quick brown fox", "the quick brown fox", "quarterly tax filing"]
    )
    assert a1 == a2
    assert abs(sum(x * x for x in a1) - 1.0) < 1e-9
    related = (await emb.embed(["a quick brown fox jumps"]))[0]
    assert cosine(a1, related) > cosine(a1, b)
    assert len(a1) == emb.dimension == 256
    empty = (await emb.embed([""]))[0]
    assert not any(empty)


def test_math_helpers() -> None:
    assert normalise([3, 4]) == [0.6, 0.8]
    assert normalise([0, 0]) == [0, 0]
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0


def test_build_embedder_selection() -> None:
    assert isinstance(
        build_embedder(Settings(_env_file=None, embedding_backend="hashing")), HashingEmbedder
    )
    with pytest.raises(ConfigurationError):
        build_embedder(Settings(_env_file=None, embedding_backend="gemini"))
    local = build_embedder(Settings(_env_file=None, embedding_backend="local_light"))
    assert local.name.startswith("local:")


# ---------------------------------------------------------------- vector stores -----------------
@pytest.fixture(params=["memory", "chroma"])
def store(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    if request.param == "memory":
        return InMemoryVectorStore()
    return ChromaVectorStore(tmp_path / "chroma")


async def _fill(store, emb: HashingEmbedder) -> None:  # type: ignore[no-untyped-def]
    texts = ["python async programming", "baking sourdough bread", "async await in python"]
    vecs = await emb.embed(texts)
    await store.upsert(
        "col",
        ["a", "b", "c"],
        vecs,
        texts,
        [{"chat_id": "1"}, {"chat_id": "2"}, {"chat_id": "1"}],
    )


async def test_vector_store_contract(store) -> None:  # type: ignore[no-untyped-def]
    emb = HashingEmbedder()
    await _fill(store, emb)
    assert await store.count("col") == 3
    q = (await emb.embed(["python async"]))[0]
    hits = await store.query("col", q, 2)
    assert {h.id for h in hits} == {"a", "c"}
    assert hits[0].score >= hits[1].score
    assert hits[0].vector is not None
    only_two = await store.query("col", q, 5, {"chat_id": "2"})
    assert [h.id for h in only_two] == ["b"]
    not_one = await store.query("col", q, 5, {"chat_id": {"$ne": "1"}})
    assert [h.id for h in not_one] == ["b"]
    both = await store.query("col", q, 5, {"chat_id": {"$in": ["1", "2"]}})
    assert len(both) == 3
    await store.delete("col", ["a"])
    assert await store.count("col") == 2
    await store.delete_where("col", {"chat_id": "1"})
    assert await store.count("col") == 1
    await store.reset("col")
    assert await store.count("col") == 0
    assert await store.query("col", q, 3) == []
    await store.upsert("col", [], [], [], [])  # no-op


async def test_upsert_replaces(store) -> None:  # type: ignore[no-untyped-def]
    emb = HashingEmbedder()
    v = (await emb.embed(["x"]))[0]
    await store.upsert("col", ["a"], [v], ["one"], [{"k": "1"}])
    await store.upsert("col", ["a"], [v], ["two"], [{"k": "1"}])
    assert await store.count("col") == 1
    assert (await store.query("col", v, 1))[0].document == "two"


def test_mmr_prefers_diversity() -> None:
    q = [1.0, 0.0]
    hits = [
        VectorHit("a", 1, "", {}, [1.0, 0.0]),
        VectorHit("b", 1, "", {}, [0.99, 0.01]),
        VectorHit("c", 1, "", {}, [0.6, 0.8]),
    ]
    picked = mmr(q, hits, 2, lambda_=0.3)
    assert [h.id for h in picked] == ["a", "c"]
    assert mmr(q, [VectorHit("x", 1, "", {}, None)], 3) == []


async def test_ensure_collection_rebuilds_on_model_change(db: Database) -> None:
    store = InMemoryVectorStore()
    calls: list[int] = []

    async def rebuild() -> int:
        calls.append(1)
        return 7

    e1 = HashingEmbedder(64)
    assert await ensure_collection(db, store, e1, "col", rebuild) is True
    assert await ensure_collection(db, store, e1, "col", rebuild) is False
    assert await ensure_collection(db, store, HashingEmbedder(128), "col", rebuild) is True
    assert len(calls) == 2


# ---------------------------------------------------------------- history -----------------------
@pytest.fixture
async def history(settings: Settings, db: Database) -> HistoryStore:
    return HistoryStore(db, settings, HashingEmbedder(), InMemoryVectorStore())


async def _chat_with(history: HistoryStore, title: str, pairs: list[tuple[str, str]], **kw) -> str:  # type: ignore[no-untyped-def]
    cid = await history.create_chat(title, **kw)
    for user, bot in pairs:
        u = await history.add_message(cid, "user", user)
        a = await history.add_message(cid, "assistant", bot)
        await history.index_exchange(cid, [u, a])
    return cid


def test_fts_query_neutralises_operators() -> None:
    assert fts_query('foo AND "bar" NEAR(x) -baz*') == '"foo" OR "and" OR "bar" OR "near" OR "baz"'
    assert fts_query("!!! ?") == ""
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 40) == 10


def test_split_exchange_overlap_and_boundaries() -> None:
    assert split_exchange("   ") == []
    assert split_exchange("short") == ["short"]
    long = "\n\n".join(f"paragraph {i} " + "word " * 120 for i in range(20))
    chunks = split_exchange(long)
    assert len(chunks) > 1
    assert all(len(c) <= 3200 + 10 for c in chunks)
    # overlap: the tail of one chunk reappears at the head of the next
    assert chunks[0][-100:].strip()[:30] in chunks[1]


async def test_chat_lifecycle(history: HistoryStore) -> None:
    cid = await history.create_chat("First")
    await history.add_message(cid, "user", "hello", meta={"a": 1}, receipts=["r1"])
    await history.add_message(cid, "assistant", "hi back")
    msgs = await history.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["meta"] == {"a": 1}
    assert msgs[0]["receipts"] == ["r1"]
    assert len(await history.get_messages(cid, limit=1)) == 1
    await history.rename_chat(cid, "  Renamed   chat  ")
    chat = await history.get_chat(cid)
    assert chat is not None
    assert chat["title"] == "Renamed chat"
    assert chat["titled"] == 1
    await history.set_archived(cid, True)
    assert await history.list_chats() == []
    assert len(await history.list_chats(include_archived=True)) == 1
    await history.delete_chat(cid)
    assert await history.get_chat(cid) is None
    assert await history.db.scalar("SELECT COUNT(*) FROM messages") == 0


async def test_keyword_semantic_and_hybrid_search(history: HistoryStore) -> None:
    c1 = await _chat_with(
        history,
        "Async talk",
        [("How do I use asyncio gather?", "Use asyncio.gather to run coroutines concurrently.")],
    )
    await _chat_with(
        history, "Bread", [("Sourdough hydration tips?", "Aim for seventy percent hydration.")]
    )
    kw = await history.keyword_search("asyncio gather")
    assert kw[0].chat_id == c1
    assert "asyncio" in kw[0].snippet.lower()
    sem = await history.semantic_search("running coroutines concurrently with asyncio")
    assert sem[0].chat_id == c1
    hybrid = await history.search("asyncio coroutines", limit=3)
    assert hybrid[0].chat_id == c1
    assert set(hybrid[0].sources) >= {"keyword"}
    assert hybrid[0].to_dict()["title"] == "Async talk"
    assert await history.search("zzzzqqq") == []
    assert await history.search("!!!") == []


async def test_search_filters_dates_and_exclusion(history: HistoryStore) -> None:
    c1 = await _chat_with(history, "One", [("alpha beta gamma", "delta")])
    c2 = await _chat_with(history, "Two", [("alpha beta gamma again", "epsilon")])
    only = await history.keyword_search("alpha", exclude_chat_id=c1)
    assert {r.chat_id for r in only} == {c2}
    assert await history.keyword_search("alpha", date_to="2000-01-01") == []
    assert await history.keyword_search("alpha", date_from="2999-01-01") == []
    assert (
        len(await history.keyword_search("alpha", date_from="2000-01-01", date_to="2999-12-31"))
        == 2
    )
    assert await history.semantic_search("alpha beta gamma", date_to="2000-01-01") == []


async def test_private_chats_never_leak(history: HistoryStore) -> None:
    secret = await _chat_with(
        history, "Secret", [("my hidden plan zebra", "noted zebra")], private=True
    )
    assert await history.keyword_search("zebra") == []
    assert await history.semantic_search("zebra") == []
    with pytest.raises(PermissionFailure):
        await history.recall_chat(secret)
    own = await history.recall_chat(secret, requesting_chat_id=secret)
    assert own["messages"]
    # flipping a public chat to private removes it from the index; flipping back restores it
    pub = await _chat_with(history, "Public", [("giraffe facts please", "tall animals")])
    assert await history.semantic_search("giraffe")
    await history.set_private(pub, True)
    assert await history.semantic_search("giraffe") == []
    assert await history.keyword_search("giraffe") == []
    await history.set_private(pub, False)
    assert await history.semantic_search("giraffe")


async def test_stale_vectors_cannot_leak_deleted_or_private_chats(
    history: HistoryStore, db: Database
) -> None:
    cid = await _chat_with(history, "Doomed", [("unicorn research notes", "ok")])
    await db.execute("UPDATE chats SET private=1 WHERE id=?", (cid,))  # bypass set_private
    assert await history.semantic_search("unicorn research") == []
    await db.execute("DELETE FROM chats WHERE id=?", (cid,))
    assert await history.semantic_search("unicorn research") == []


async def test_passive_recall_threshold_and_switch(
    history: HistoryStore, settings: Settings
) -> None:
    current = await history.create_chat("Now")
    other = await _chat_with(
        history,
        "Earlier",
        [("configure postgres replication slots", "use pg_create_logical_replication_slot")],
    )
    hits = await history.passive_recall("postgres replication slots", current)
    assert [h.chat_id for h in hits] == [other]
    assert (
        await history.passive_recall("wholly unrelated gardening query", current, threshold=0.9)
        == []
    )
    assert (
        await history.passive_recall("postgres replication slots", other) == []
    )  # excludes own chat
    settings.passive_cross_chat_recall = False
    assert await history.passive_recall("postgres replication slots", current) == []


async def test_recall_chat_caps_and_errors(history: HistoryStore) -> None:
    cid = await history.create_chat("Long")
    for i in range(60):
        await history.add_message(cid, "user" if i % 2 == 0 else "assistant", f"message {i}")
    await history.add_message(cid, "tool", "tool noise")
    out = await history.recall_chat(cid)
    assert len(out["messages"]) == 40
    assert out["total_messages"] == 61
    part = await history.recall_chat(cid, start=58, end=61)
    assert [m["text"] for m in part["messages"]] == ["message 58", "message 59"]
    with pytest.raises(NotFound):
        await history.recall_chat("chat_missing")


async def test_rebuild_index_after_model_change(
    history: HistoryStore, settings: Settings, db: Database
) -> None:
    await _chat_with(history, "A", [("rebuild me please", "sure")])
    await _chat_with(history, "B", [("private stuff here", "ok")], private=True)
    fresh = HistoryStore(db, settings, HashingEmbedder(64), InMemoryVectorStore())
    assert await fresh.rebuild_index() == 1  # only the public chat
    assert await fresh.semantic_search("rebuild me")


async def test_history_without_embedder_still_keyword_searches(
    settings: Settings, db: Database
) -> None:
    plain = HistoryStore(db, settings)
    cid = await plain.create_chat("K")
    await plain.add_message(cid, "user", "findable keyword zephyr")
    assert await plain.index_exchange(cid, []) == 0
    assert (await plain.search("zephyr"))[0].chat_id == cid
    assert await plain.semantic_search("zephyr") == []
