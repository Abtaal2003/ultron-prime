"""Drift monitoring (spec 4.6).

Each reply gets a persona-consistency score: its embedding's similarity to the centroid of the
original "Ultron" contrast replies minus its similarity to the centroid of the generic-assistant
replies, mapped to ``[0, 1]`` (0.5 means equally close to both). Heuristic persona breaks subtract
a penalty. A sustained decline (the rolling mean over the last few replies below a floor, or
several low scores in a row) triggers a notice and charter reinforcement on the next turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.db import Database, new_id
from src.core.events import utcnow_iso
from src.core.memory.embeddings import Embedder, cosine, normalise
from src.core.persona.loader import ContrastPair

WINDOW = 6
FLOOR = 0.5
LOW_RUN = 3
PENALTY = 0.2

REINFORCEMENT = (
    "Persona drift was detected in recent replies. Return to the register: dry, cold, exact, "
    "witty. No cheerful openers, no sign-offs offering more help. Answer first."
)


@dataclass(slots=True)
class DriftState:
    """Rolling drift information for one chat."""

    scores: list[float]
    rolling: float
    declining: bool

    @property
    def reinforce(self) -> bool:
        """Whether the next turn should carry the reinforcement text."""
        return self.declining


def _centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    return normalise([sum(v[i] for v in vectors) / len(vectors) for i in range(dim)])


class StyleScorer:
    """Scores a reply against the Ultron and generic centroids of the contrast pairs."""

    def __init__(self, embedder: Embedder, pairs: list[ContrastPair]) -> None:
        self.embedder = embedder
        self.pairs = pairs
        self._ultron: list[float] = []
        self._generic: list[float] = []

    async def prepare(self) -> None:
        """Embed the contrast pairs once."""
        if self._ultron or not self.pairs:
            return
        ultron = await self.embedder.embed([p.ultron for p in self.pairs], "document")
        generic = await self.embedder.embed([p.generic for p in self.pairs], "document")
        self._ultron, self._generic = _centroid(ultron), _centroid(generic)

    async def score(self, reply: str, *, persona_issues: int = 0) -> float:
        """Persona-consistency score for ``reply`` in ``[0, 1]`` (higher is more Ultron)."""
        await self.prepare()
        if not self._ultron:
            return 1.0
        vec = (await self.embedder.embed([reply[:2000]], "document"))[0]
        margin = cosine(vec, self._ultron) - cosine(vec, self._generic)
        value = (margin + 1.0) / 2.0 - PENALTY * persona_issues
        return max(0.0, min(1.0, value))


class DriftMonitor:
    """Scores replies and tracks decline."""

    def __init__(self, db: Database, embedder: Embedder, pairs: list[ContrastPair]) -> None:
        self.db = db
        self.embedder = embedder
        self.pairs = pairs
        self.style = StyleScorer(embedder, pairs)

    async def prepare(self) -> None:
        """Embed the contrast pairs once."""
        await self.style.prepare()

    async def score(self, reply: str, *, persona_issues: int = 0) -> float:
        """Persona-consistency score for ``reply`` in ``[0, 1]`` (higher is more Ultron)."""
        return await self.style.score(reply, persona_issues=persona_issues)

    async def record(self, chat_id: str, turn_id: str | None, score: float) -> None:
        """Persist a score."""
        await self.db.execute(
            "INSERT INTO drift_scores(id, chat_id, turn_id, score, created_at) VALUES (?,?,?,?,?)",
            (new_id("drf"), chat_id, turn_id, score, utcnow_iso()),
        )

    async def state(self, chat_id: str) -> DriftState:
        """Rolling mean and the decline verdict from the last ``WINDOW`` scores."""
        rows = await self.db.fetchall(
            "SELECT score FROM drift_scores WHERE chat_id=? ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?",
            (chat_id, WINDOW),
        )
        scores = [float(r["score"]) for r in reversed(rows)]
        rolling = sum(scores) / len(scores) if scores else 1.0
        low_run = len(scores) >= LOW_RUN and all(s < FLOOR for s in scores[-LOW_RUN:])
        declining = low_run or (len(scores) >= WINDOW // 2 and rolling < FLOOR)
        return DriftState(scores=scores, rolling=rolling, declining=declining)
