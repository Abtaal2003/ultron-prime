"""Embedding backends and vector stores (spec 7.7, 10).

SQLite is the source of truth. A vector store is a rebuildable index: when the configured
embedding model or its dimension changes, the collection is dropped and re-embedded from SQLite.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from src.config import Settings
from src.core.db import Database
from src.core.errors import ConfigurationError, ProviderUnavailable
from src.core.events import utcnow_iso
from src.logging_setup import get_logger

log = get_logger("embeddings")

Kind = Literal["document", "query"]
Where = dict[str, Any]


class Embedder(Protocol):
    """Turns text into unit-length vectors."""

    name: str
    dimension: int

    async def embed(self, texts: Sequence[str], kind: Kind = "document") -> list[list[float]]:
        """Embed ``texts``; every vector has length ``dimension``."""
        ...


def normalise(vec: Sequence[float]) -> list[float]:
    """Scale ``vec`` to unit length (a zero vector stays zero)."""
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else list(vec)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_WORD = re.compile(r"[\w']+", re.UNICODE)


class HashingEmbedder:
    """Deterministic feature-hashing embedder for tests and low-resource machines.

    Words and character trigrams are hashed into a fixed number of signed buckets. It captures
    lexical overlap only (no synonyms), which is enough to exercise every code path offline.
    """

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.name = f"hashing-{dimension}"

    def _features(self, text: str) -> list[str]:
        words = [w.lower() for w in _WORD.findall(text)]
        feats = list(words)
        for w in words:
            padded = f"#{w}#"
            feats.extend(padded[i : i + 3] for i in range(max(len(padded) - 2, 0)))
        return feats

    async def embed(self, texts: Sequence[str], kind: Kind = "document") -> list[list[float]]:
        """Hash each text into a signed bag of features."""
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimension
            for feat in self._features(text):
                digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dimension
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign
            out.append(normalise(vec))
        return out


class LocalEmbedder:
    """``sentence-transformers`` model run on this machine (lazy import, worker thread)."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.name = f"local:{model_name}"
        self._model: Any = None
        self.dimension = 0

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - optional extra
                raise ConfigurationError(
                    "local embeddings need the 'embeddings' extra: uv sync --extra embeddings"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
            self.dimension = int(self._model.get_sentence_embedding_dimension())
        return self._model

    async def embed(self, texts: Sequence[str], kind: Kind = "document") -> list[list[float]]:
        """Encode in a worker thread so the event loop stays responsive."""

        def run() -> list[list[float]]:
            model = self._load()
            prompt = (
                "query" if kind == "query" and "query" in getattr(model, "prompts", {}) else None
            )
            vectors = model.encode(
                list(texts), normalize_embeddings=True, prompt_name=prompt, show_progress_bar=False
            )
            return [[float(x) for x in v] for v in vectors]

        return await asyncio.to_thread(run)


class GeminiEmbedder:
    """Gemini embeddings API. Sends text to Google; selected explicitly in Settings."""

    def __init__(self, api_key: str, model: str, dimension: int = 768, client: Any | None = None):
        self.name = f"gemini:{model}"
        self.model = model
        self.dimension = dimension
        if client is not None:
            self._client = client
        else:
            from google import genai

            self._client = genai.Client(api_key=api_key)

    async def embed(self, texts: Sequence[str], kind: Kind = "document") -> list[list[float]]:
        """Embed with the task type matching ``kind``."""
        from google.genai import types

        task = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
        try:
            response = await self._client.aio.models.embed_content(
                model=self.model,
                contents=list(texts),
                config=types.EmbedContentConfig(
                    task_type=task, output_dimensionality=self.dimension
                ),
            )
        except Exception as exc:
            raise ProviderUnavailable(
                "embedding request failed", detail=type(exc).__name__
            ) from exc
        return [normalise(list(e.values)) for e in response.embeddings]


def build_embedder(settings: Settings) -> Embedder:
    """Create the embedder selected by ``EMBEDDING_BACKEND``."""
    backend = settings.embedding_backend
    if backend == "hashing":
        return HashingEmbedder()
    if backend == "gemini":
        key = settings.gemini_api_key.get_secret_value()
        if not key:
            raise ConfigurationError("EMBEDDING_BACKEND=gemini needs GEMINI_API_KEY")
        return GeminiEmbedder(key, settings.gemini_embedding_model)
    model = {
        "local": settings.local_embedding_model,
        "local_light": "sentence-transformers/all-MiniLM-L6-v2",
        "local_alt": "google/embeddinggemma-300m",
    }[backend]
    return LocalEmbedder(model)


# --------------------------------------------------------------------------- vector stores ----


@dataclass(slots=True)
class VectorHit:
    """One nearest-neighbour result (``score`` is cosine similarity, higher is closer)."""

    id: str
    score: float
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)
    vector: list[float] | None = None


class VectorStore(Protocol):
    """A rebuildable nearest-neighbour index."""

    async def upsert(
        self,
        collection: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]],
    ) -> None:
        """Insert or replace items."""
        ...

    async def query(
        self, collection: str, vector: Sequence[float], k: int, where: Where | None = None
    ) -> list[VectorHit]:
        """Return up to ``k`` nearest items (vectors included) matching ``where``."""
        ...

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        """Remove items by id."""
        ...

    async def delete_where(self, collection: str, where: Where) -> None:
        """Remove items whose metadata match ``where``."""
        ...

    async def count(self, collection: str) -> int:
        """Number of items."""
        ...

    async def reset(self, collection: str) -> None:
        """Drop every item in the collection."""
        ...


def _matches(meta: dict[str, Any], where: Where | None) -> bool:
    """Evaluate the small filter language shared by both stores: equality, $ne, $in, $nin."""
    if not where:
        return True
    for key, cond in where.items():
        value = meta.get(key)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$ne" and value == arg:
                    return False
                if op == "$in" and value not in arg:
                    return False
                if op == "$nin" and value in arg:
                    return False
                if op == "$eq" and value != arg:
                    return False
        elif value != cond:
            return False
    return True


class InMemoryVectorStore:
    """Exact cosine search in memory. Used by tests and as a no-dependency fallback."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, tuple[list[float], str, dict[str, Any]]]] = {}

    async def upsert(
        self,
        collection: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]],
    ) -> None:
        """Insert or replace items."""
        bucket = self._data.setdefault(collection, {})
        for i, v, d, m in zip(ids, vectors, documents, metadatas, strict=True):
            bucket[i] = (list(v), d, dict(m))

    async def query(
        self, collection: str, vector: Sequence[float], k: int, where: Where | None = None
    ) -> list[VectorHit]:
        """Exact nearest neighbours."""
        hits = [
            VectorHit(id=i, score=cosine(vector, v), document=d, metadata=dict(m), vector=v)
            for i, (v, d, m) in self._data.get(collection, {}).items()
            if _matches(m, where)
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        """Remove items by id."""
        bucket = self._data.get(collection, {})
        for i in ids:
            bucket.pop(i, None)

    async def delete_where(self, collection: str, where: Where) -> None:
        """Remove items matching ``where``."""
        bucket = self._data.get(collection, {})
        for i in [i for i, (_, _, m) in bucket.items() if _matches(m, where)]:
            del bucket[i]

    async def count(self, collection: str) -> int:
        """Number of items."""
        return len(self._data.get(collection, {}))

    async def reset(self, collection: str) -> None:
        """Drop every item."""
        self._data[collection] = {}


class ChromaVectorStore:
    """Persistent Chroma index (cosine space). Calls run in worker threads."""

    def __init__(self, path: Path) -> None:
        import chromadb

        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))

    def _collection(self, name: str) -> Any:
        return self._client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})

    @staticmethod
    def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            k: (v if isinstance(v, str | int | float | bool) else str(v))
            for k, v in meta.items()
            if v is not None
        }

    async def upsert(
        self,
        collection: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]],
    ) -> None:
        """Insert or replace items."""
        if not ids:
            return

        def run() -> None:
            self._collection(collection).upsert(
                ids=list(ids),
                embeddings=[list(v) for v in vectors],
                documents=list(documents),
                metadatas=[self._clean_meta(m) or {"_": 1} for m in metadatas],
            )

        await asyncio.to_thread(run)

    async def query(
        self, collection: str, vector: Sequence[float], k: int, where: Where | None = None
    ) -> list[VectorHit]:
        """Approximate nearest neighbours with metadata filtering."""

        def run() -> list[VectorHit]:
            col = self._collection(collection)
            total = col.count()
            if total == 0:
                return []
            result = col.query(
                query_embeddings=[list(vector)],
                n_results=min(k, total),
                where=_chroma_where(where),
                include=["documents", "metadatas", "distances", "embeddings"],
            )
            hits: list[VectorHit] = []
            for i, doc, meta, dist, emb in zip(
                result["ids"][0],
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
                result["embeddings"][0],
                strict=True,
            ):
                hits.append(
                    VectorHit(
                        id=i,
                        score=1.0 - float(dist),
                        document=doc or "",
                        metadata=dict(meta or {}),
                        vector=[float(x) for x in emb],
                    )
                )
            return hits

        return await asyncio.to_thread(run)

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        """Remove items by id."""
        if ids:
            await asyncio.to_thread(lambda: self._collection(collection).delete(ids=list(ids)))

    async def delete_where(self, collection: str, where: Where) -> None:
        """Remove items matching ``where``."""
        await asyncio.to_thread(
            lambda: self._collection(collection).delete(where=_chroma_where(where))
        )

    async def count(self, collection: str) -> int:
        """Number of items."""
        return int(await asyncio.to_thread(lambda: self._collection(collection).count()))

    async def reset(self, collection: str) -> None:
        """Drop and recreate the collection."""

        def run() -> None:
            try:
                self._client.delete_collection(collection)
            except Exception:  # absent collection
                log.debug("chroma_collection_absent", collection=collection)
            self._collection(collection)

        await asyncio.to_thread(run)


def _chroma_where(where: Where | None) -> dict[str, Any] | None:
    if not where:
        return None
    clauses = [{k: v} if isinstance(v, dict) else {k: {"$eq": v}} for k, v in where.items()]
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def build_vector_store(settings: Settings) -> VectorStore:
    """Chroma when installed, otherwise the in-memory store (with a warning)."""
    try:
        return ChromaVectorStore(settings.chroma_dir)
    except ImportError:  # pragma: no cover - chromadb is a core dependency
        log.warning("chroma_unavailable_using_memory_store")
        return InMemoryVectorStore()


# ------------------------------------------------------------------------- index lifecycle ----

RebuildFn = Callable[[], Awaitable[int]]


async def ensure_collection(
    db: Database, store: VectorStore, embedder: Embedder, collection: str, rebuild: RebuildFn
) -> bool:
    """Make ``collection`` consistent with the active embedder.

    Compares the stored ``embedding_model`` and ``dimension`` with the embedder. On a mismatch (or
    a first run) the collection is reset and ``rebuild`` re-embeds everything from SQLite.

    Returns:
        ``True`` when a rebuild ran.
    """
    row = await db.fetchone(
        "SELECT embedding_model, dimension FROM embedding_index_meta WHERE collection=?",
        (collection,),
    )
    dim = embedder.dimension or len((await embedder.embed(["probe"]))[0])
    if row is not None and row["embedding_model"] == embedder.name and row["dimension"] == dim:
        return False
    await store.reset(collection)
    count = await rebuild()
    await db.execute(
        "INSERT INTO embedding_index_meta(collection, embedding_model, dimension, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(collection) DO UPDATE SET embedding_model=excluded."
        "embedding_model, dimension=excluded.dimension, updated_at=excluded.updated_at",
        (collection, embedder.name, dim, utcnow_iso()),
    )
    log.info("collection_rebuilt", collection=collection, items=count, model=embedder.name)
    return True


def mmr(
    query: Sequence[float],
    hits: Sequence[VectorHit],
    k: int,
    lambda_: float = 0.7,
) -> list[VectorHit]:
    """Maximal marginal relevance over hits that carry vectors (relevance vs diversity)."""
    chosen: list[VectorHit] = []
    pool = [h for h in hits if h.vector is not None]
    while pool and len(chosen) < k:
        best: VectorHit | None = None
        best_score = -math.inf
        for h in pool:
            assert h.vector is not None
            relevance = cosine(query, h.vector)
            redundancy = max(
                (cosine(h.vector, c.vector) for c in chosen if c.vector is not None), default=0.0
            )
            score = lambda_ * relevance - (1 - lambda_) * redundancy
            if score > best_score:
                best, best_score = h, score
        assert best is not None
        chosen.append(best)
        pool.remove(best)
    return chosen
