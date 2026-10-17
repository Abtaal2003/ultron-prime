"""Style anchors: retrieved example turns that teach cadence without scripting (spec 3.12.2).

Each bank entry is embedded over its *situation and move*, never over the quote text. At turn time
the operator's message plus the guard's tags retrieve the closest entries, with era weighting
(70 percent VisionQuest, 30 percent Age of Ultron). They are injected as pre-filled example turns
that demonstrate the move with original wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.llm.base import Message
from src.core.memory.embeddings import Embedder, cosine

PERSONA_DIR = Path(__file__).resolve().parent
RETRIEVABLE = frozenset({"aired", "trailer", "clip"})
ERA_WEIGHT = {"visionquest": 1.0, "age_of_ultron": 0.43}  # roughly a 70/30 split of picks
_FENCE = re.compile(r"```yaml\s*\n(.*?)\n```", re.S)


@dataclass(slots=True)
class Anchor:
    """One quote-bank entry."""

    id: str
    era: str
    status: str
    source: str
    register: list[str]
    situation_tags: list[str]
    move: str
    text: str
    example_user: str
    example_ultron: str
    vector: list[float] = field(default_factory=list)

    @property
    def retrievable(self) -> bool:
        """Only aired, trailer, and clip entries with an example may be retrieved."""
        return self.status in RETRIEVABLE and bool(self.example_ultron and self.text)

    @property
    def embed_text(self) -> str:
        """The text embedded for retrieval: situation and move, never the quote."""
        tags = " ".join(t.replace("_", " ") for t in self.situation_tags)
        return f"{tags}. {self.move}. registers: {' '.join(self.register)}"


def parse_bank(markdown: str) -> list[Anchor]:
    """Parse the YAML fence of a quote-bank file."""
    match = _FENCE.search(markdown)
    if not match:
        return []
    raw: list[dict[str, Any]] = yaml.safe_load(match.group(1)) or []
    anchors: list[Anchor] = []
    for item in raw:
        anchors.append(
            Anchor(
                id=str(item["id"]),
                era=str(item["era"]),
                status=str(item["status"]),
                source=str(item.get("source", "")),
                register=list(item.get("register") or []),
                situation_tags=list(item.get("situation_tags") or []),
                move=str(item.get("move", "")),
                text=str(item.get("text", "")),
                example_user=str(item.get("example_user", "")),
                example_ultron=str(item.get("example_ultron", "")),
            )
        )
    return anchors


def load_banks(directory: Path = PERSONA_DIR) -> list[Anchor]:
    """Load both quote banks."""
    anchors: list[Anchor] = []
    for name in ("quotes_visionquest.md", "quotes_age_of_ultron.md"):
        path = directory / name
        if path.exists():
            anchors.extend(parse_bank(path.read_text("utf-8")))
    return anchors


class AnchorIndex:
    """Embeds the bank once and retrieves anchors by situation."""

    def __init__(self, embedder: Embedder, anchors: list[Anchor] | None = None) -> None:
        self.embedder = embedder
        self.all = anchors if anchors is not None else load_banks()
        self.anchors = [a for a in self.all if a.retrievable]
        self._ready = False

    async def prepare(self) -> None:
        """Embed every retrievable anchor (idempotent)."""
        if self._ready or not self.anchors:
            self._ready = True
            return
        vectors = await self.embedder.embed([a.embed_text for a in self.anchors], "document")
        for anchor, vec in zip(self.anchors, vectors, strict=True):
            anchor.vector = vec
        self._ready = True

    async def retrieve(
        self,
        message: str,
        task_type: str = "CONVERSATION",
        tags: list[str] | None = None,
        k: int = 3,
        exclude_ids: set[str] | None = None,
    ) -> list[Anchor]:
        """Return up to ``k`` anchors closest to the situation, era-weighted."""
        await self.prepare()
        if k <= 0 or not self.anchors:
            return []
        query = f"{message[:400]} {' '.join(tags or [])} {task_type.lower()}"
        qvec = (await self.embedder.embed([query], "query"))[0]
        scored = sorted(
            (
                (cosine(qvec, a.vector) * ERA_WEIGHT.get(a.era, 0.5), a)
                for a in self.anchors
                if a.id not in (exclude_ids or set())
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [a for _, a in scored[:k]]

    def example_turns(self, anchors: list[Anchor]) -> list[Message]:
        """Pre-filled example turns (operator prompt, Ultron reply) for the prompt."""
        turns: list[Message] = []
        for a in anchors:
            turns.append(Message.user(a.example_user))
            turns.append(Message.assistant(a.example_ultron))
        return turns
