"""Check memory and history recall against a real embedding model (no LLM calls).

The vault and history use fixed cosine cut-offs (memory recall 0.3, passive cross-chat recall
0.35, history search 0.2, duplicate memory 0.9). Cosine scales differ between models, so this
measures, for one embedding backend, how often the right memory clears each cut-off and how
often an unrelated one does. It also prints the repeat-detection calibration for the model.

    uv run python -m evals.embeddings_check --backend local_light [--write]

``--write`` stores the repeat thresholds in ``data/calibration.json``. CI runs this from the
``embeddings`` workflow, which can download models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from src.config import Settings
from src.core.memory.embeddings import Embedder, build_embedder, cosine

from evals.repeat import calibrate, write

MEMORIES = (
    "The operator always writes tests with pytest, never unittest.",
    "The operator never deploys on Fridays.",
    "The operator's expense tracker is a FastAPI service called ledger.",
    "The operator prefers short answers without bullet lists.",
    "The operator's home server runs Debian on a Raspberry Pi.",
    "The operator is learning Rust in the evenings.",
)

RELATED = (  # (query, index of the memory it should find)
    ("How should I test this function?", 0),
    ("Write unit tests for parse_expense.", 0),
    ("Is it fine to ship the release this Friday evening?", 1),
    ("When's a good day to deploy?", 1),
    ("What's my service called again?", 2),
    ("Add an endpoint to the ledger app.", 2),
    ("Keep it brief.", 3),
    ("Can you set up a cron job on my Pi?", 4),
    ("Which language am I studying at night?", 5),
)

UNRELATED = (
    "What's the capital of Portugal?",
    "Explain how photosynthesis works.",
    "Recommend a good science fiction novel.",
    "How do I make a sourdough starter?",
    "What does HTTP 418 mean?",
    "Translate 'good morning' into Japanese.",
    "Who painted The Garden of Earthly Delights?",
    "How far is the Moon from the Earth?",
)

NEAR_DUPLICATES = (
    ("The operator uses pytest for every test and never unittest.", 0),
    ("Operator rule: no deployments on Fridays.", 1),
)

CUTOFFS = {"history_search": 0.2, "memory_recall": 0.3, "passive_recall": 0.35}
DUPLICATE = 0.9


@dataclass(slots=True)
class Report:
    """What one backend scored."""

    embedder: str
    related_top1: float
    related_scores: list[float]
    unrelated_max: list[float]
    by_cutoff: dict[str, dict[str, float]]
    duplicates_caught: int
    duplicate_scores: list[float]


async def measure(embedder: Embedder) -> Report:
    """Score every query against every memory."""
    mem = await embedder.embed(list(MEMORIES), "document")
    rel = await embedder.embed([q for q, _ in RELATED], "query")
    unr = await embedder.embed(list(UNRELATED), "query")
    dup = await embedder.embed([t for t, _ in NEAR_DUPLICATES], "document")

    def scores(vec: list[float]) -> list[float]:
        return [cosine(vec, m) for m in mem]

    related_scores, top1 = [], 0
    for vec, (_, want) in zip(rel, RELATED, strict=True):
        s = scores(vec)
        related_scores.append(round(s[want], 3))
        top1 += max(range(len(s)), key=s.__getitem__) == want
    unrelated_max = [round(max(scores(v)), 3) for v in unr]
    by_cutoff = {
        name: {
            "related_found": round(sum(x >= cut for x in related_scores) / len(RELATED), 3),
            "unrelated_pulled_in": round(sum(x >= cut for x in unrelated_max) / len(UNRELATED), 3),
        }
        for name, cut in CUTOFFS.items()
    }
    dup_scores = [
        round(cosine(v, mem[i]), 3) for v, (_, i) in zip(dup, NEAR_DUPLICATES, strict=True)
    ]
    return Report(
        embedder=embedder.name,
        related_top1=round(top1 / len(RELATED), 3),
        related_scores=related_scores,
        unrelated_max=unrelated_max,
        by_cutoff=by_cutoff,
        duplicates_caught=sum(s >= DUPLICATE for s in dup_scores),
        duplicate_scores=dup_scores,
    )


async def run(backend: str, write_calibration: bool) -> dict[str, object]:
    """Measure one backend and calibrate repeat detection for it."""
    settings = Settings(_env_file=None, embedding_backend=backend)
    embedder = build_embedder(settings)
    report = await measure(embedder)
    calibration = await calibrate(embedder)
    if write_calibration:
        write(calibration)
    return {"recall": asdict(report), "repeat": calibration.to_dict()}


def main() -> None:
    """CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="local_light")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.backend, args.write)), indent=2))


if __name__ == "__main__":
    main()
