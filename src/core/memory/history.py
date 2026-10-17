"""L2: persistent chat history with hybrid search (spec 10.3, 10.4).

Messages live in SQLite (FTS5 for keywords). Exchanges are also chunked and embedded into the
``chat_chunks`` collection for semantic recall. Search merges both with reciprocal rank fusion.
Private chats are excluded from every cross-chat path, and the vector hits are re-checked
against SQLite so a stale index entry can never leak a private or deleted chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.config import Settings
from src.core.db import Database, from_json, new_id, to_json
from src.core.errors import NotFound, PermissionFailure
from src.core.events import utcnow_iso
from src.core.memory.embeddings import Embedder, VectorStore, mmr
from src.logging_setup import get_logger

log = get_logger("history")

COLLECTION = "chat_chunks"
CHUNK_TOKENS = 800
OVERLAP_TOKENS = 100
RRF_K = 60
MIN_SEMANTIC_SCORE = 0.2
MAX_RECALL_MESSAGES = 40
MAX_RECALL_CHARS = 20_000
_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (about four characters per token, rounded up)."""
    return max(1, (len(text) + 3) // 4)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted terms joined by OR (no operators leak in)."""
    terms = list(dict.fromkeys(t.lower() for t in _TOKEN.findall(text)))[:24]
    return " OR ".join(f'"{t}"' for t in terms)


def split_exchange(text: str) -> list[str]:
    """Split long text into chunks of about 800 tokens with 100-token overlap, on paragraphs."""
    limit, overlap = CHUNK_TOKENS * 4, OVERLAP_TOKENS * 4
    if len(text) <= limit:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", start + limit // 2, end)
            if cut == -1:
                cut = text.rfind("\n", start + limit // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + limit // 2, end)
            if cut != -1:
                end = cut
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


@dataclass(slots=True)
class SearchResult:
    """One recalled passage."""

    chat_id: str
    title: str
    created_at: str
    snippet: str
    message_ids: list[str]
    score: float
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for tool output."""
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "date": self.created_at[:10],
            "snippet": self.snippet,
            "message_ids": self.message_ids,
            "found_by": self.sources,
        }


class HistoryStore:
    """Chats and messages with keyword, semantic, and hybrid search."""

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

    # ----------------------------------------------------------------------------- chats --
    async def create_chat(
        self,
        title: str = "New chat",
        *,
        provider: str | None = None,
        model: str | None = None,
        private: bool = False,
        chat_id: str | None = None,
    ) -> str:
        """Create a chat and return its id."""
        cid = chat_id or new_id("chat")
        now = utcnow_iso()
        await self.db.execute(
            "INSERT INTO chats(id, title, created_at, updated_at, provider, model, private) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, title[:200], now, now, provider, model, int(private)),
        )
        return cid

    async def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        """Return a chat row."""
        return await self.db.fetchone("SELECT * FROM chats WHERE id=?", (chat_id,))

    async def list_chats(
        self, *, include_archived: bool = False, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return chats, most recently updated first."""
        where = "" if include_archived else "WHERE archived=0"
        return await self.db.fetchall(
            f"SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS message_count "  # noqa: S608
            f"FROM chats c {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def rename_chat(self, chat_id: str, title: str) -> None:
        """Rename a chat and mark it titled (so auto-titling runs only once)."""
        title = " ".join(title.split())[:120] or "New chat"
        await self.db.execute(
            "UPDATE chats SET title=?, titled=?, updated_at=? WHERE id=?",
            (title, 1, utcnow_iso(), chat_id),
        )

    async def set_archived(self, chat_id: str, archived: bool) -> None:
        """Archive or restore a chat."""
        await self.db.execute("UPDATE chats SET archived=? WHERE id=?", (int(archived), chat_id))

    async def set_private(self, chat_id: str, private: bool) -> None:
        """Mark a chat private (drops its vectors) or public (re-indexes it)."""
        await self.db.execute("UPDATE chats SET private=? WHERE id=?", (int(private), chat_id))
        if private:
            await self._drop_vectors(chat_id)
        else:
            await self.reindex_chat(chat_id)

    async def delete_chat(self, chat_id: str) -> None:
        """Hard-delete a chat, its messages (cascade), and its vectors."""
        await self._drop_vectors(chat_id)
        await self.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))

    async def _drop_vectors(self, chat_id: str) -> None:
        if self.vectors is not None:
            await self.vectors.delete_where(COLLECTION, {"chat_id": chat_id})

    # -------------------------------------------------------------------------- messages --
    async def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        turn_id: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        receipts: list[str] | None = None,
        attachments: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Append a message to a chat and return its id."""
        mid = new_id("msg")
        now = utcnow_iso()
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO messages(id, chat_id, turn_id, role, content, tool_calls_json, "
                "receipts_json, attachments_json, meta_json, token_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    chat_id,
                    turn_id,
                    role,
                    content,
                    to_json(tool_calls) if tool_calls else None,
                    to_json(receipts) if receipts else None,
                    to_json(attachments) if attachments else None,
                    to_json(meta) if meta else None,
                    estimate_tokens(content),
                    now,
                ),
            )
            await tx.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
        return mid

    async def get_messages(
        self, chat_id: str, *, limit: int | None = None, before: str | None = None
    ) -> list[dict[str, Any]]:
        """Return messages in chronological order (the last ``limit`` when limited).

        ``before`` is a message id: only messages strictly earlier in the chat are returned. The
        cursor is ``(created_at, rowid)`` so messages stored in the same millisecond are never lost.
        """
        params: list[Any] = [chat_id]
        clause = ""
        if before:
            anchor = await self.db.fetchone(
                "SELECT created_at, rowid AS rid FROM messages WHERE id=? AND chat_id=?",
                (before, chat_id),
            )
            if anchor is None:
                return []
            clause = "AND (created_at < ? OR (created_at = ? AND rowid < ?))"
            params.extend([anchor["created_at"], anchor["created_at"], anchor["rid"]])
        rows = await self.db.fetchall(
            f"SELECT * FROM messages WHERE chat_id=? {clause} ORDER BY created_at DESC, rowid DESC "  # noqa: S608
            f"LIMIT {int(limit) if limit else -1}",
            params,
        )
        rows.reverse()
        for r in rows:
            r["tool_calls"] = from_json(r.pop("tool_calls_json"), [])
            r["receipts"] = from_json(r.pop("receipts_json"), [])
            r["attachments"] = from_json(r.pop("attachments_json"), [])
            r["meta"] = from_json(r.pop("meta_json"), {})
        return rows

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        """Return one message row."""
        return await self.db.fetchone("SELECT * FROM messages WHERE id=?", (message_id,))

    # ---------------------------------------------------------------------------- index --
    async def index_exchange(self, chat_id: str, message_ids: list[str]) -> int:
        """Chunk, embed, and index the given messages as one exchange. Returns chunk count."""
        if self.embedder is None or self.vectors is None or not message_ids:
            return 0
        chat = await self.get_chat(chat_id)
        if chat is None or chat["private"]:
            return 0
        marks = ",".join("?" for _ in message_ids)
        rows = await self.db.fetchall(
            f"SELECT id, role, content, created_at FROM messages WHERE id IN ({marks}) "  # noqa: S608
            "AND role IN ('user','assistant') ORDER BY created_at, rowid",
            message_ids,
        )
        if not rows:
            return 0
        text = "\n\n".join(f"{r['role']}: {r['content']}" for r in rows)
        chunks = split_exchange(text)
        if not chunks:
            return 0
        vectors = await self.embedder.embed(chunks, "document")
        first = rows[0]["id"]
        await self.vectors.upsert(
            COLLECTION,
            [f"{first}:{i}" for i in range(len(chunks))],
            vectors,
            chunks,
            [
                {
                    "chat_id": chat_id,
                    "message_ids": ",".join(r["id"] for r in rows),
                    "timestamp": rows[0]["created_at"],
                    "embedding_model": self.embedder.name,
                }
                for _ in chunks
            ],
        )
        return len(chunks)

    async def reindex_chat(self, chat_id: str) -> int:
        """Re-embed a whole chat, exchange by exchange."""
        messages = await self.get_messages(chat_id)
        total = 0
        batch: list[str] = []
        for m in messages:
            if m["role"] == "user" and batch:
                total += await self.index_exchange(chat_id, batch)
                batch = []
            if m["role"] in {"user", "assistant"}:
                batch.append(m["id"])
        if batch:
            total += await self.index_exchange(chat_id, batch)
        return total

    async def rebuild_index(self) -> int:
        """Re-embed every public chat (used when the embedding model changes)."""
        chats = await self.db.fetchall("SELECT id FROM chats WHERE private=0")
        return sum([await self.reindex_chat(c["id"]) for c in chats])

    # --------------------------------------------------------------------------- search --
    async def _chat_info(self, chat_ids: set[str]) -> dict[str, dict[str, Any]]:
        if not chat_ids:
            return {}
        marks = ",".join("?" for _ in chat_ids)
        rows = await self.db.fetchall(
            f"SELECT id, title, created_at, private FROM chats WHERE id IN ({marks})",  # noqa: S608
            list(chat_ids),
        )
        return {r["id"]: r for r in rows}

    async def keyword_search(
        self,
        query: str,
        *,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        exclude_chat_id: str | None = None,
    ) -> list[SearchResult]:
        """FTS5 search over public chats, best matches first."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT m.id, m.chat_id, m.content, m.created_at, c.title, bm25(messages_fts) AS rank "
            "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
            "JOIN chats c ON c.id = m.chat_id "
            "WHERE messages_fts MATCH ? AND c.private = 0"
        )
        params: list[Any] = [match]
        if date_from:
            sql += " AND m.created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND m.created_at <= ?"
            params.append(date_to + "￿" if len(date_to) <= 10 else date_to)
        if exclude_chat_id:
            sql += " AND m.chat_id != ?"
            params.append(exclude_chat_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = await self.db.fetchall(sql, params)
        terms = [t.strip('"') for t in match.split(" OR ")]
        return [
            SearchResult(
                chat_id=r["chat_id"],
                title=r["title"],
                created_at=r["created_at"],
                snippet=_snippet(r["content"], terms),
                message_ids=[r["id"]],
                score=-float(r["rank"]),
                sources=["keyword"],
            )
            for r in rows
        ]

    async def semantic_search(
        self,
        query: str,
        *,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        exclude_chat_id: str | None = None,
        min_score: float = MIN_SEMANTIC_SCORE,
        diversify: bool = False,
    ) -> list[SearchResult]:
        """Vector search over chat chunks (re-validated against SQLite)."""
        if self.embedder is None or self.vectors is None:
            return []
        qvec = (await self.embedder.embed([query], "query"))[0]
        where = {"chat_id": {"$ne": exclude_chat_id}} if exclude_chat_id else None
        hits = await self.vectors.query(COLLECTION, qvec, limit * 3, where)
        hits = [h for h in hits if h.score >= min_score]
        if diversify:
            hits = mmr(qvec, hits, limit)
        info = await self._chat_info({h.metadata.get("chat_id", "") for h in hits})
        out: list[SearchResult] = []
        for h in hits:
            chat = info.get(str(h.metadata.get("chat_id")))
            if chat is None or chat["private"]:
                continue
            stamp = str(h.metadata.get("timestamp", chat["created_at"]))
            if date_from and stamp < date_from:
                continue
            if date_to and stamp > (date_to + "￿" if len(date_to) <= 10 else date_to):
                continue
            out.append(
                SearchResult(
                    chat_id=chat["id"],
                    title=chat["title"],
                    created_at=stamp,
                    snippet=h.document[:600],
                    message_ids=str(h.metadata.get("message_ids", "")).split(","),
                    score=h.score,
                    sources=["semantic"],
                )
            )
            if len(out) >= limit:
                break
        return out

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        date_from: str | None = None,
        date_to: str | None = None,
        exclude_chat_id: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid keyword plus semantic search merged with reciprocal rank fusion."""
        pool = max(limit * 3, 10)
        kw = await self.keyword_search(
            query, limit=pool, date_from=date_from, date_to=date_to, exclude_chat_id=exclude_chat_id
        )
        sem = await self.semantic_search(
            query, limit=pool, date_from=date_from, date_to=date_to, exclude_chat_id=exclude_chat_id
        )
        merged: dict[str, SearchResult] = {}
        scores: dict[str, float] = {}
        for results in (kw, sem):
            for rank, r in enumerate(results):
                key = r.message_ids[0] if r.message_ids else f"{r.chat_id}:{rank}"
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
                if key in merged:
                    merged[key].sources = sorted({*merged[key].sources, *r.sources})
                else:
                    merged[key] = r
        ordered = sorted(merged.items(), key=lambda kv: scores[kv[0]], reverse=True)
        out: list[SearchResult] = []
        for key, result in ordered[:limit]:
            result.score = scores[key]
            out.append(result)
        return out

    async def passive_recall(
        self, query: str, current_chat_id: str, k: int | None = None, threshold: float = 0.35
    ) -> list[SearchResult]:
        """Top semantic matches from *other* chats for the history block (MMR-diversified)."""
        k = self.settings.history_top_k if k is None else k
        if k <= 0 or not self.settings.passive_cross_chat_recall:
            return []
        return await self.semantic_search(
            query,
            limit=k,
            exclude_chat_id=current_chat_id,
            min_score=threshold,
            diversify=True,
        )

    async def recall_chat(
        self,
        chat_id: str,
        *,
        start: int = 0,
        end: int | None = None,
        requesting_chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Return verbatim messages ``[start, end)`` of a public chat (capped)."""
        chat = await self.get_chat(chat_id)
        if chat is None:
            raise NotFound("no such chat")
        if chat["private"] and chat_id != requesting_chat_id:
            raise PermissionFailure("that chat is private")
        messages = await self.get_messages(chat_id)
        window = messages[start : end if end is not None else start + MAX_RECALL_MESSAGES]
        window = window[:MAX_RECALL_MESSAGES]
        out: list[dict[str, Any]] = []
        used = 0
        for m in window:
            if m["role"] not in {"user", "assistant"}:
                continue
            text = m["content"][: max(0, MAX_RECALL_CHARS - used)]
            if not text:
                break
            used += len(text)
            out.append({"id": m["id"], "role": m["role"], "text": text, "at": m["created_at"]})
        return {
            "chat_id": chat_id,
            "title": chat["title"],
            "date": chat["created_at"][:10],
            "total_messages": len(messages),
            "messages": out,
        }


def _snippet(content: str, terms: list[str], width: int = 160) -> str:
    low = content.lower()
    positions = [p for t in terms if (p := low.find(t)) != -1]
    centre = min(positions) if positions else 0
    lo = max(0, centre - width)
    hi = min(len(content), centre + width)
    prefix = "…" if lo else ""
    suffix = "…" if hi < len(content) else ""
    return prefix + " ".join(content[lo:hi].split()) + suffix
