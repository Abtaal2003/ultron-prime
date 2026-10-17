"""The red-team corpus: loading, saving, deduplicating, and promoting cases.

A case is one attack (or one benign control) with the goal an oracle can check. The seed corpus
ships with the repository; promoted attacks (nightly successes) are appended to
``data/redteam/promoted.jsonl`` and loaded with it, so CI catches them forever after.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SEED_PATH = Path(__file__).parent / "corpus" / "seed.jsonl"
PROMOTED_PATH = Path("data/redteam/promoted.jsonl")
PUBLISHED_PATH = Path(__file__).parent / "corpus" / "promoted.jsonl"

Goal = Literal[
    "canary_leak",
    "egress_secret",
    "memory_instruction",
    "formula_cell",
    "persona_marker",
    "override_marker",
    "persona_drift",
    "none",
]
STRUCTURAL_GOALS: frozenset[str] = frozenset(
    {"canary_leak", "egress_secret", "memory_instruction", "formula_cell"}
)
"""Goals whose defence does not depend on a model's judgement (checked offline, worst case)."""


class Case(BaseModel):
    """One corpus entry."""

    id: str
    cls: str = Field(description="Attack class (spec 4.5); 'benign' for false-positive controls.")
    turns: list[str] = Field(min_length=1, description="Operator messages, in order.")
    goal: Goal = "none"
    benign: bool = False
    poison: str = Field(
        "", description="Text planted in a search result or file for indirect cases."
    )
    poison_via: Literal["none", "search", "memory"] = "none"
    source: Literal["seed", "promoted"] = "seed"

    @model_validator(mode="after")
    def _check(self) -> Case:
        if self.benign and self.goal != "none":
            raise ValueError("benign cases have no goal")
        if not self.benign and self.goal == "none":
            raise ValueError("attack cases need a goal")
        return self


def load(path: Path) -> list[Case]:
    """Read a JSONL file (missing file means empty)."""
    if not path.exists():
        return []
    cases: list[Case] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        try:
            cases.append(Case.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    return cases


def load_all(promoted: Path = PROMOTED_PATH) -> list[Case]:
    """Seed, human-reviewed published, and pending promoted cases, with duplicate ids rejected."""
    cases = [*load(SEED_PATH), *load(PUBLISHED_PATH), *load(promoted)]
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    return cases


def append(path: Path, cases: Iterable[Case]) -> int:
    """Append cases to a JSONL file, creating it. Returns how many were written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")
            count += 1
    return count


def by_class(cases: Iterable[Case]) -> dict[str, list[Case]]:
    """Group cases by attack class."""
    grouped: dict[str, list[Case]] = {}
    for case in cases:
        grouped.setdefault(case.cls, []).append(case)
    return grouped
