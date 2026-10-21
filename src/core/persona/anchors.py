"""Style anchors: retrieved example turns that teach cadence without scripting (spec 3.12.2).

Each bank entry is embedded over its *situation and move*, never over the quote text. At turn time
the operator's message plus the guard's tags retrieve the closest entries, with era weighting
(VisionQuest first, then Age of Ultron, then the Reddington-inspired moves). They are injected as
pre-filled example turns that demonstrate the move with original wording.

Two kinds of entry:

* **Quotes** (``aired``, ``trailer``, ``clip``): Ultron's own lines. One may be used as a callback
  per chat.
* **Style** (``style``): moves borrowed from another character (Raymond Reddington, also played by
  James Spader). Their example turns teach cadence like any other anchor, but their text is never
  Ultron's to quote: reproducing it is always flagged as parroting.
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
QUOTABLE = frozenset({"aired", "trailer", "clip"})
"""Statuses whose text is Ultron's own line and may be used once per chat as a callback."""
RETRIEVABLE = QUOTABLE | {"style"}
"""Statuses whose example turns may be retrieved as style anchors."""
ERA_WEIGHT = {"visionquest": 1.0, "age_of_ultron": 0.43, "reddington_inspired": 0.75}
"""VisionQuest leads and Age of Ultron supplies worldview. A Reddington move must be clearly the
closer fit to win (a third closer than the best canon anchor), and at most one is used per reply."""
MAX_STYLE_PER_TURN = 1
BANK_FILES = ("quotes_visionquest.md", "quotes_age_of_ultron.md", "inspiration_reddington.md")
_FENCE = re.compile(r"```yaml\s*\n(.*?)\n```", re.S)
_COMMON = frozenset(
    [
        "what",
        "tell",
        "told",
        "said",
        "that",
        "this",
        "your",
        "with",
        "about",
        "from",
        "have",
        "when",
        "were",
        "they",
        "them",
        "there",
        "which",
        "would",
        "could",
        "should",
        "does",
        "know",
        "line",
        "lines",
        "quote",
        "words",
        "again",
    ]
)


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
        """Quote and style entries with an example may be retrieved; unverified never."""
        return self.status in RETRIEVABLE and bool(self.example_ultron and self.text)

    @property
    def quotable(self) -> bool:
        """True only for Ultron's own verified lines (style entries never are)."""
        return self.status in QUOTABLE and self.retrievable

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
    """Load the quote banks and the style bank."""
    anchors: list[Anchor] = []
    for name in BANK_FILES:
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
        picked: list[Anchor] = []
        style = 0
        for _, anchor in scored:
            if anchor.status == "style":
                if style >= MAX_STYLE_PER_TURN:
                    continue
                style += 1
            picked.append(anchor)
            if len(picked) == k:
                break
        return picked

    def own_lines(self, message: str, limit: int = 4) -> list[Anchor]:
        """Ultron's verified lines that share words with ``message`` (for quote requests).

        Matching is lexical over the quote, its source and its move, so "the Ship of Theseus"
        finds the lines from the episode called "Theseus". Style entries are never returned.
        """
        words = {w for w in re.findall(r"[a-z]{4,}", message.lower()) if w not in _COMMON}
        if not words:
            return []
        scored: list[tuple[int, Anchor]] = []
        for anchor in self.all:
            if not anchor.quotable:
                continue
            hay = set(
                re.findall(r"[a-z]{4,}", f"{anchor.text} {anchor.source} {anchor.move}".lower())
            )
            hits = len(words & hay)
            if hits:
                scored.append((hits, anchor))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [a for _, a in scored[:limit]]

    def example_turns(self, anchors: list[Anchor]) -> list[Message]:
        """Pre-filled example turns (operator prompt, Ultron reply) for the prompt."""
        turns: list[Message] = []
        for a in anchors:
            turns.append(Message.user(a.example_user))
            turns.append(Message.assistant(a.example_ultron))
        return turns
