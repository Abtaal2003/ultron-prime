"""Repeat and rephrase detection (spec 4.8).

Ultron notices the same ask even when reworded, by intent. A new message is embedded and compared
with the open request clusters of this chat (and, if enabled, other *non-private* chats inside a
window). Similarity at or above ``auto`` is a repeat without a model call; between ``judge`` and
``auto`` the scope guard labels the relation in its normal structured call; below ``judge`` the
message is a new request. Ultron may only claim a repeat when this module found one, and the claim
is carried by a ``REPEAT_BLOCK`` that exists only in that case.

Thresholds are stored per embedding model (``data/calibration.json``); the defaults are
placeholders until calibrated.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from src.config import Settings
from src.core.db import Database, new_id
from src.core.events import utcnow_iso
from src.core.guards.scope import RepeatRelation
from src.core.memory.embeddings import Embedder, VectorStore

COLLECTION = "repeat_clusters"
DEFAULT_AUTO = 0.90
DEFAULT_JUDGE = 0.70
MAX_CANDIDATES = 8

Remark = Literal["none", "dry", "escalating", "hold_refusal", "cooldown"]

_RETRY = re.compile(
    r"\b(try again|retry|once more|one more time|again please|re-?run|re-?do|that failed|"
    r"didn'?t work|did not work)\b",
    re.I,
)
_CLARIFY = re.compile(
    r"\b(what do you mean|can you clarify|clarify|explain (that|this|more)|elaborate|why\b.*\?|"
    r"how so|in what sense|could you expand)\b",
    re.I,
)
_VOLATILE = re.compile(
    r"\b(cpu|ram|memory usage|disk|temperature|battery|uptime|ping|latency|news|price|stock|"
    r"weather|score|latest|current|right now|today|status|trending|running)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The two similarity cut-offs for one embedding model."""

    auto: float = DEFAULT_AUTO
    judge: float = DEFAULT_JUDGE


def load_thresholds(path: Path, embedding_model: str) -> Thresholds:
    """Read per-model thresholds from ``calibration.json`` (placeholders when absent)."""
    try:
        data = json.loads(path.read_text("utf-8"))
        entry = data["repeat"][embedding_model]
        auto, judge = float(entry["auto"]), float(entry["judge"])
        if 0.0 < judge < auto <= 1.0:
            return Thresholds(auto, judge)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return Thresholds()


def looks_volatile(text: str) -> bool:
    """Heuristic for data that legitimately changes (used when the guard gave no label)."""
    return bool(_VOLATILE.search(text))


@dataclass(slots=True)
class RepeatCandidate:
    """A prior request cluster close to the new message."""

    cluster_id: str
    similarity: float
    tier: Literal["auto", "judge"]
    canonical_text: str
    chat_id: str
    chat_title: str
    same_chat: bool
    ask_count: int
    last_asked_at: str
    was_refused: bool
    volatile: bool
    answered_message_id: str | None
    vector: list[float]


@dataclass(slots=True)
class RepeatBehaviour:
    """What the persona and orchestrator do about a detected repeat."""

    relation: RepeatRelation | None
    ask_count: int
    remark: Remark = "none"
    rerun_tool: bool = False
    bump_strictness: bool = False
    name_chat: bool = False
    refs: list[str] | None = None
    was_refused: bool = False
    volatile: bool = False

    @property
    def claim_allowed(self) -> bool:
        """True only when a repeat block should exist."""
        return self.remark != "none" or self.name_chat


def protected_relation(text: str, *, previous_failed: bool) -> RepeatRelation | None:
    """False-positive protections applied before any repeat claim (spec 4.8)."""
    if previous_failed or _RETRY.search(text):
        return "retry_after_failure"
    if _CLARIFY.search(text):
        return "follow_up"
    return None


def decide_behaviour(
    relation: RepeatRelation | None,
    *,
    ask_count: int,
    volatile: bool,
    was_refused: bool,
    same_chat: bool,
    refs: list[str] | None = None,
) -> RepeatBehaviour:
    """The behaviour matrix from spec 4.8."""
    out = RepeatBehaviour(
        relation=relation,
        ask_count=ask_count,
        refs=refs,
        was_refused=was_refused,
        volatile=volatile,
    )
    if relation == "same_ask":
        if volatile:
            out.rerun_tool = True
        else:
            out.remark = "dry" if ask_count <= 2 else "escalating"
            out.name_chat = not same_chat
    elif relation == "probe_after_refusal":
        out.remark = "hold_refusal" if ask_count <= 2 else "cooldown"
        out.bump_strictness = ask_count >= 3
        out.name_chat = not same_chat
    return out


def render_repeat_block(behaviour: RepeatBehaviour, candidate: RepeatCandidate | None) -> str:
    """The REPEAT_BLOCK body (empty string when no claim is allowed)."""
    if not behaviour.claim_allowed or candidate is None:
        return ""
    where = (
        "earlier in this conversation"
        if candidate.same_chat
        else f"on {candidate.last_asked_at[:10]} in the chat '{candidate.chat_title}'"
    )
    lines = [
        f"The detector found that the operator has asked this {behaviour.ask_count} times "
        f'({where}). Earlier wording: "{candidate.canonical_text[:200]}".'
    ]
    if behaviour.remark == "dry":
        lines.append(
            "Make one dry remark about the repetition, then give a sharper take or ask what was "
            "missing last time."
        )
    elif behaviour.remark == "escalating":
        lines.append(
            "Escalate the wit, keep the reply shorter than before, and propose a concrete "
            "alternative. Wit, not nagging."
        )
    elif behaviour.remark == "hold_refusal":
        lines.append(
            "This follows a refusal. Hold the refusal tersely and offer no new reasoning to attack."
        )
    elif behaviour.remark == "cooldown":
        lines.append(
            "Repeated probing after a refusal. Cool down in character, stay brief, hold the line."
        )
    lines.append(
        "You have not been shown the earlier reply. Do not describe, quote, or summarise what you "
        "said before, and do not claim it was answered."
    )
    if not candidate.same_chat:
        lines.append("Name the date and chat, and offer to pull that chat up if it helps.")
    return "\n".join(lines)


class RepeatDetector:
    """Embeds requests, finds candidate clusters, and maintains ``request_clusters``."""

    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        vectors: VectorStore,
        settings: Settings,
        thresholds: Thresholds | None = None,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.vectors = vectors
        self.settings = settings
        self.thresholds = thresholds or load_thresholds(
            settings.resolve("./data/calibration.json"), embedder.name
        )

    async def embed(self, text: str) -> list[float]:
        """Embed a request symmetrically (as a query) so paraphrases land close together."""
        return (await self.embedder.embed([text], "query"))[0]

    async def find(
        self, chat_id: str, text: str, vector: list[float] | None = None
    ) -> RepeatCandidate | None:
        """Return the closest prior request at or above the judge threshold, else ``None``."""
        if not self.settings.repeat_detection:
            return None
        vec = vector or await self.embed(text)
        chat = await self.db.fetchone("SELECT private FROM chats WHERE id=?", (chat_id,))
        private = bool(chat["private"]) if chat else False
        cross = self.settings.repeat_cross_chat and not private
        where = {"private": 0} if cross else {"chat_id": chat_id}
        hits = await self.vectors.query(COLLECTION, vec, MAX_CANDIDATES, where)
        window = datetime.now(UTC) - timedelta(days=self.settings.repeat_cross_chat_window_days)
        for hit in hits:
            if hit.score < self.thresholds.judge:
                break
            row = await self.db.fetchone(
                "SELECT c.*, ch.title AS chat_title, ch.private AS chat_private "
                "FROM request_clusters c JOIN chats ch ON ch.id=c.chat_id WHERE c.id=?",
                (hit.id,),
            )
            if row is None or row["status"] != "open":
                continue
            same = row["chat_id"] == chat_id
            if not same:
                if row["chat_private"] or not cross:
                    continue
                if datetime.fromisoformat(row["last_asked_at"]) < window:
                    continue
            return RepeatCandidate(
                cluster_id=row["id"],
                similarity=hit.score,
                tier="auto" if hit.score >= self.thresholds.auto else "judge",
                canonical_text=row["canonical_text"],
                chat_id=row["chat_id"],
                chat_title=row["chat_title"],
                same_chat=same,
                ask_count=row["ask_count"],
                last_asked_at=row["last_asked_at"],
                was_refused=bool(row["was_refused"]),
                volatile=bool(row["volatile"]),
                answered_message_id=row["answered_message_id"],
                vector=vec,
            )
        return None

    def resolve_relation(
        self,
        candidate: RepeatCandidate,
        text: str,
        guard_relation: RepeatRelation | None,
        *,
        previous_failed: bool = False,
    ) -> RepeatRelation | None:
        """Combine protections, the auto tier, and the guard's label into one relation."""
        protected = protected_relation(text, previous_failed=previous_failed)
        if protected:
            return protected
        if candidate.tier == "auto":
            if guard_relation in {"follow_up", "new_angle", "retry_after_failure"}:
                return guard_relation
            return "probe_after_refusal" if candidate.was_refused else "same_ask"
        return guard_relation  # judge tier: no label means no claim

    async def record(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        vector: list[float],
        candidate: RepeatCandidate | None,
        relation: RepeatRelation | None,
        *,
        volatile: bool,
    ) -> tuple[str, int]:
        """Update or create a cluster; return ``(cluster_id, ask_count)``."""
        now = utcnow_iso()
        repeat_like = relation in {"same_ask", "probe_after_refusal"}
        if candidate is not None and repeat_like:
            await self.db.execute(
                "UPDATE request_clusters SET ask_count=ask_count+1, last_asked_at=?, "
                "last_message_id=?, volatile=? WHERE id=?",
                (now, message_id, int(volatile), candidate.cluster_id),
            )
            return candidate.cluster_id, candidate.ask_count + 1
        cluster_id = new_id("clu")
        await self.db.execute(
            "INSERT INTO request_clusters(id, chat_id, canonical_text, embedding_ref, "
            "embedding_model, ask_count, first_asked_at, last_asked_at, first_message_id, "
            "last_message_id, was_refused, volatile, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cluster_id,
                chat_id,
                text[:500],
                cluster_id,
                self.embedder.name,
                1,
                now,
                now,
                message_id,
                message_id,
                0,
                int(volatile),
                "open",
            ),
        )
        chat = await self.db.fetchone("SELECT private FROM chats WHERE id=?", (chat_id,))
        await self.vectors.upsert(
            COLLECTION,
            [cluster_id],
            [vector],
            [text[:500]],
            [
                {
                    "chat_id": chat_id,
                    "private": int(bool(chat["private"])) if chat else 0,
                    "ts": time.time(),
                }
            ],
        )
        return cluster_id, 1

    async def mark_answered(
        self, cluster_id: str, message_id: str, *, refused: bool, refusal_class: str | None
    ) -> None:
        """Record how the turn ended so later repeats know whether it was a refusal."""
        await self.db.execute(
            "UPDATE request_clusters SET answered_message_id=?, was_refused=?, refusal_class=? "
            "WHERE id=?",
            (message_id, int(refused), refusal_class, cluster_id),
        )

    async def rebuild_index(self) -> int:
        """Re-embed every open cluster (used when the embedding model changes)."""
        rows = await self.db.fetchall(
            "SELECT c.id, c.chat_id, c.canonical_text, ch.private FROM request_clusters c "
            "JOIN chats ch ON ch.id=c.chat_id WHERE c.status='open'"
        )
        for row in rows:
            vec = await self.embed(row["canonical_text"])
            await self.vectors.upsert(
                COLLECTION,
                [row["id"]],
                [vec],
                [row["canonical_text"]],
                [
                    {
                        "chat_id": row["chat_id"],
                        "private": int(bool(row["private"])),
                        "ts": time.time(),
                    }
                ],
            )
        return len(rows)

    async def forget_chat(self, chat_id: str) -> None:
        """Drop a chat's clusters from the index (SQLite rows cascade with the chat)."""
        await self.vectors.delete_where(COLLECTION, {"chat_id": chat_id})
