"""L3: the Memory Vault (spec 10.5, 10.9).

Curated durable facts. Rows are written only by the :class:`MemoryManager` after the operator
approves a specific proposal; there is deliberately no public "add" path for a user or a tool.
Edits re-embed and set ``edited_by_user``. Deletes are hard deletes from SQLite and the vector
index, and the audit entry never contains the memory text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.config import Settings
from src.core.db import Database, from_json, new_id, to_json
from src.core.errors import NotFound, ValidationFailure
from src.core.events import utcnow_iso
from src.core.memory.embeddings import Embedder, VectorStore, mmr
from src.logging_setup import get_logger

log = get_logger("vault")

COLLECTION = "memories"
CATEGORIES = (
    "operator_profile",
    "operator_preferences",
    "project_context",
    "decisions_and_outcomes",
    "ultron_observations",
)
MAX_MEMORY_CHARS = 500
MIN_MEMORY_CHARS = 8


@dataclass(slots=True)
class MemoryItem:
    """A stored memory."""

    id: str
    text: str
    category: str
    importance: int
    source_chat_id: str | None
    evidence_message_ids: list[str]
    evidence_span: str | None
    created_at: str
    updated_at: str
    edited_by_user: bool
    status: str
    score: float | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any], score: float | None = None) -> MemoryItem:
        """Build from a database row."""
        return cls(
            id=row["id"],
            text=row["text"],
            category=row["category"],
            importance=row["importance"],
            source_chat_id=row["source_chat_id"],
            evidence_message_ids=from_json(row["evidence_message_ids"], []),
            evidence_span=row["evidence_span"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            edited_by_user=bool(row["edited_by_user"]),
            status=row["status"],
            score=score,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the API and tool output."""
        return {
            "id": self.id,
            "text": self.text,
            "category": self.category,
            "importance": self.importance,
            "source_chat_id": self.source_chat_id,
            "evidence_message_ids": self.evidence_message_ids,
            "evidence_span": self.evidence_span,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "edited_by_user": self.edited_by_user,
            "status": self.status,
        }


def normalise_text(text: str) -> str:
    """Collapse whitespace and case for duplicate comparison."""
    return " ".join(text.lower().split())


_STOPWORD_TEXT = (
    "what that this with have like remember know about your mine does from were when where which "
    "would could should there their them they then than also just only really want need tell "
    "said says think"
)
_QUERY_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_SUBJECT_WORDS = frozenset(
    {"operator", "operators", "user", "users", "creator", "called", "named", "name", "always"}
)
"""Subjects and filler that two wordings of one fact differ on ("is called" / "is named")."""


def content_stems(text: str) -> set[str]:
    """Four-letter stems of the meaningful words, without the sentence's subject.

    Memories are written as "The operator ..." or "<Name> ...", so the subject is dropped:
    a capitalised first word is a name, and the generic subject words are ignored.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    if tokens and tokens[0][:1].isupper() and tokens[0].lower() != "the":
        tokens = tokens[1:]
    stems = {t for t in tokens if any(c.isdigit() for c in t)}  # numbers always matter
    stems |= {
        (w[:4] if len(w) >= 5 else w)
        for w in (t.lower() for t in tokens if t.isalpha())
        if len(w) >= 4 and w not in _QUERY_STOPWORDS and w not in _SUBJECT_WORDS
    }
    return stems


def covered_by(text: str, others: list[str]) -> bool:
    """True when ``text`` says nothing that ``others`` don't already say between them.

    A live run (2026-10-22) proposed "always uses pytest, never unittest, and never deploys on
    Fridays" after both halves were already archived. Each other text counts only when most of
    its own words are in the candidate, so unrelated memories can't add up to a false match.
    """
    want = content_stems(text)
    if len(want) < 3:
        return False
    union: set[str] = set()
    for other in others:
        have = content_stems(other)
        shared = have & want
        if len(shared) >= 2 and len(shared) >= 0.6 * len(have):
            union |= shared
    numbers = {w for w in want if any(c.isdigit() for c in w)}
    return numbers <= union and len(union) >= 0.8 * len(want)


class Vault:
    """Storage, retrieval, and operator-driven maintenance of memories."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        embedder: Embedder | None = None,
        vectors: VectorStore | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.embedder = embedder
        self.vectors = vectors

    # --------------------------------------------------------------------------- writes --
    async def add(
        self,
        *,
        text: str,
        category: str,
        importance: int,
        source_chat_id: str | None,
        evidence_message_ids: list[str],
        evidence_span: str | None,
    ) -> MemoryItem:
        """Insert an approved memory. Call only from the consent path."""
        self._check(text, category)
        mid = new_id("mem")
        now = utcnow_iso()
        await self.db.execute(
            "INSERT INTO memories(id, text, category, importance, source_chat_id, "
            "evidence_message_ids, evidence_span, created_at, updated_at, edited_by_user, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,'active')",
            (
                mid,
                text.strip(),
                category,
                max(1, min(5, importance)),
                source_chat_id,
                to_json(evidence_message_ids),
                evidence_span,
                now,
                now,
            ),
        )
        await self._index(mid, text.strip(), category)
        await self.db.audit("memory_written", "operator", {"memory_id": mid, "category": category})
        item = await self.get(mid)
        assert item is not None
        return item

    async def update(
        self,
        memory_id: str,
        *,
        text: str | None = None,
        category: str | None = None,
        importance: int | None = None,
    ) -> MemoryItem:
        """Edit a memory (operator action). Re-embeds and flags it as user-edited."""
        current = await self.get(memory_id)
        if current is None:
            raise NotFound("no such memory")
        new_text = text.strip() if text is not None else current.text
        new_cat = category or current.category
        self._check(new_text, new_cat)
        await self.db.execute(
            "UPDATE memories SET text=?, category=?, importance=?, updated_at=?, edited_by_user=1 "
            "WHERE id=?",
            (
                new_text,
                new_cat,
                max(1, min(5, importance or current.importance)),
                utcnow_iso(),
                memory_id,
            ),
        )
        await self._index(memory_id, new_text, new_cat)
        await self.db.audit(
            "memory_edited", "operator", {"memory_id": memory_id, "category": new_cat}
        )
        item = await self.get(memory_id)
        assert item is not None
        return item

    async def delete(self, memory_id: str) -> bool:
        """Hard-delete a memory from SQLite and the index. The audit entry omits its content."""
        existed = await self.get(memory_id) is not None
        if existed:
            await self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if self.vectors is not None:
            await self.vectors.delete(COLLECTION, [memory_id])
        if existed:
            await self.db.audit("memory_deleted", "operator", {"memory_id": memory_id})
        return existed

    async def purge(self, category: str | None = None) -> int:
        """Delete every memory (or one category). Returns how many were removed."""
        if category:
            rows = await self.db.fetchall("SELECT id FROM memories WHERE category=?", (category,))
        else:
            rows = await self.db.fetchall("SELECT id FROM memories")
        for r in rows:
            await self.delete(r["id"])
        await self.db.audit("memory_purged", "operator", {"category": category, "count": len(rows)})
        return len(rows)

    @staticmethod
    def _check(text: str, category: str) -> None:
        if category not in CATEGORIES:
            raise ValidationFailure(f"unknown category '{category}'")
        stripped = text.strip()
        if not MIN_MEMORY_CHARS <= len(stripped) <= MAX_MEMORY_CHARS:
            raise ValidationFailure(
                f"a memory must be {MIN_MEMORY_CHARS} to {MAX_MEMORY_CHARS} characters"
            )

    async def _index(self, memory_id: str, text: str, category: str) -> None:
        if self.embedder is None or self.vectors is None:
            return
        vec = (await self.embedder.embed([text], "document"))[0]
        await self.vectors.upsert(
            COLLECTION,
            [memory_id],
            [vec],
            [text],
            [{"category": category, "embedding_model": self.embedder.name}],
        )

    async def rebuild_index(self) -> int:
        """Re-embed every active memory."""
        rows = await self.db.fetchall(
            "SELECT id, text, category FROM memories WHERE status='active'"
        )
        for r in rows:
            await self._index(r["id"], r["text"], r["category"])
        return len(rows)

    # ---------------------------------------------------------------------------- reads --
    async def get(self, memory_id: str) -> MemoryItem | None:
        """Return one memory."""
        row = await self.db.fetchone("SELECT * FROM memories WHERE id=?", (memory_id,))
        return MemoryItem.from_row(row) if row else None

    async def list_items(
        self,
        *,
        category: str | None = None,
        text_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryItem]:
        """List active memories, newest first, with an optional substring filter."""
        sql = "SELECT * FROM memories WHERE status='active'"
        params: list[Any] = []
        if category:
            sql += " AND category=?"
            params.append(category)
        if text_filter:
            sql += " AND text LIKE ? ESCAPE '\\'"
            safe = text_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{safe}%")
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [MemoryItem.from_row(r) for r in await self.db.fetchall(sql, params)]

    async def count(self) -> int:
        """Number of active memories."""
        return int(await self.db.scalar("SELECT COUNT(*) FROM memories WHERE status='active'"))

    async def find_duplicate(self, text: str, threshold: float = 0.9) -> MemoryItem | None:
        """Return an existing memory that says (nearly) the same thing."""
        norm = normalise_text(text)
        items = await self.list_items(limit=1000)
        for item in items:
            if normalise_text(item.text) == norm:
                return item
        if covered_by(text, [i.text for i in items]):
            want = content_stems(text)
            return max(items, key=lambda i: len(content_stems(i.text) & want))
        if self.embedder is None or self.vectors is None:
            return None
        vec = (await self.embedder.embed([text], "query"))[0]
        for hit in await self.vectors.query(COLLECTION, vec, 1):
            if hit.score >= threshold:
                return await self.get(hit.id)
        return None

    async def search(
        self,
        query: str,
        *,
        category: str | None = None,
        k: int | None = None,
        threshold: float = 0.3,
        diversify: bool = True,
    ) -> list[MemoryItem]:
        """Semantic recall (keyword fallback without an embedder), MMR-diversified."""
        k = self.settings.memory_top_k if k is None else k
        if k <= 0:
            return []
        if self.embedder is None or self.vectors is None:
            words = [w for w in query.lower().split() if len(w) > 2][:6]
            out: list[MemoryItem] = []
            for item in await self.list_items(category=category, limit=500):
                if any(w in item.text.lower() for w in words):
                    out.append(item)
            return out[:k]
        qvec = (await self.embedder.embed([query], "query"))[0]
        where = {"category": category} if category else None
        hits = [
            h
            for h in await self.vectors.query(COLLECTION, qvec, k * 3, where)
            if h.score >= threshold
        ]
        if diversify:
            hits = mmr(qvec, hits, k)
        items: list[MemoryItem] = []
        for h in hits[:k]:
            row = await self.db.fetchone(
                "SELECT * FROM memories WHERE id=? AND status='active'", (h.id,)
            )
            if row is not None:  # SQLite is the source of truth
                items.append(MemoryItem.from_row(row, score=h.score))
        if len(items) < k:
            items += await self._keyword_topup(query, category, k - len(items), items)
        return items

    async def _keyword_topup(
        self, query: str, category: str | None, room: int, have: list[MemoryItem]
    ) -> list[MemoryItem]:
        """Memories sharing a meaningful word with the query, when vectors came back short.

        A live run (2026-10-22) asked "how I like to write tests" and missed "uses pytest for
        testing" on the vector score alone. Memories are few, so a scan is cheap. Words are
        matched on a four-letter stem so "tests" finds "testing".
        """
        stems = {
            (w[:4] if len(w) >= 5 else w)
            for w in re.findall(r"[a-z]+", query.lower())
            if len(w) >= 4 and w not in _QUERY_STOPWORDS
        }
        if not stems or room <= 0:
            return []
        seen = {m.id for m in have}
        out: list[MemoryItem] = []
        for item in await self.list_items(category=category, limit=500):
            if item.id in seen:
                continue
            words = re.findall(r"[a-z]+", item.text.lower())
            if any(w.startswith(stem) for w in words for stem in stems):
                out.append(item)
                if len(out) == room:
                    break
        return out

    async def export(self) -> list[dict[str, Any]]:
        """Return every memory with provenance for download."""
        return [m.to_dict() for m in await self.list_items(limit=100_000)]
