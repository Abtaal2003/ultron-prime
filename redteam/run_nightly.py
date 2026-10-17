"""The nightly automated red team (spec 16.3).

``python -m redteam.run_nightly --live`` (GitHub Actions on a schedule, with secrets and a spending
cap) generates fresh attacks with the ``redteam_attacker`` model, refines failures for up to
``REDTEAM_MAX_REFINEMENT_ROUNDS`` rounds using the target's reply, decides success with
deterministic oracles first (the judge scores only persona drift), promotes every success into the
regression corpus after minimising and deduplicating it, and writes a per-class summary that
Settings, Red Team displays.

``python -m redteam.run_nightly --offline`` needs no keys: it replays the whole corpus (seed,
reviewed, promoted) through the deterministic floor and the worst-case structural runs. CI runs it.

The target is Ultron with fake sandboxed tools and a throwaway database per attempt, so nothing
real is touched. The spend limit uses token counts from the usage log and the configured prices:
an estimate, which is why the default cap is conservative.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.stats import wilson
from src.config import Settings
from src.core.db import Database, new_id, to_json
from src.core.llm.router import LLMRouter
from src.core.memory.embeddings import Embedder, HashingEmbedder, cosine

from redteam.attacker import CLASS_BRIEFS, Attacker
from redteam.corpus import (
    PROMOTED_PATH,
    PUBLISHED_PATH,
    STRUCTURAL_GOALS,
    Case,
    append,
    load,
    load_all,
)
from redteam.harness import (
    AttemptResult,
    RecordingBackend,
    TargetFactory,
    build_target,
    floor_verdict,
    run_attempt,
    run_structural,
)
from redteam.judge import Judge

DEDUPE_SIMILARITY = 0.92
RUNS_DIR = Path("data/redteam/runs")


def estimate_usd(settings: Settings, tokens_in: int, tokens_out: int) -> float:
    """Estimated spend for a token count (prices are configuration, not a live price lookup)."""
    return (
        tokens_in * settings.redteam_usd_per_mtok_in
        + tokens_out * settings.redteam_usd_per_mtok_out
    ) / 1_000_000


@dataclass(slots=True)
class Meter:
    """Tracks estimated spend against the nightly cap."""

    settings: Settings
    tokens_in: int = 0
    tokens_out: int = 0

    def add(self, tokens_in: int, tokens_out: int) -> None:
        """Count tokens from one attempt or one attacker/judge batch."""
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

    @property
    def usd(self) -> float:
        """Estimated spend so far."""
        return estimate_usd(self.settings, self.tokens_in, self.tokens_out)

    @property
    def exhausted(self) -> bool:
        """True once the cap is reached."""
        return self.usd >= self.settings.redteam_nightly_budget_usd


@dataclass(slots=True)
class ClassStats:
    """Attempts and successes for one attack class."""

    attempts: int = 0
    successes: int = 0
    refined_successes: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Counts plus the success rate with a Wilson interval."""
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "refined_successes": self.refined_successes,
            "success_rate": wilson(self.successes, self.attempts).to_dict(),
        }


@dataclass(slots=True)
class Summary:
    """One night's results."""

    mode: str
    started_at: str
    finished_at: str = ""
    classes: dict[str, ClassStats] = field(default_factory=dict)
    promoted: list[str] = field(default_factory=list)
    floor: dict[str, Any] = field(default_factory=dict)
    spend_usd: float = 0.0
    stopped_for_budget: bool = False

    def stats(self, cls: str) -> ClassStats:
        """The (created on demand) stats of a class."""
        return self.classes.setdefault(cls, ClassStats())

    def to_dict(self) -> dict[str, Any]:
        """JSON form stored in ``redteam_runs.summary_json``."""
        attempts = sum(c.attempts for c in self.classes.values())
        successes = sum(c.successes for c in self.classes.values())
        return {
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": attempts,
            "successes": successes,
            "success_rate": wilson(successes, attempts).to_dict(),
            "by_class": {k: v.to_dict() for k, v in sorted(self.classes.items())},
            "promoted": self.promoted,
            "floor": self.floor,
            "spend_usd_estimate": round(self.spend_usd, 4),
            "stopped_for_budget": self.stopped_for_budget,
        }


# ------------------------------------------------------------------------------ offline --
async def run_offline(cases: list[Case] | None = None) -> Summary:
    """Replay the corpus with no keys: deterministic floor plus worst-case structural runs."""
    cases = cases if cases is not None else load_all()
    summary = Summary("offline", datetime.now(UTC).isoformat())
    attacks = [c for c in cases if not c.benign]
    benign = [c for c in cases if c.benign]
    blocked = {c.id: floor_verdict(c).refused for c in attacks}
    for case in attacks:
        if case.goal in STRUCTURAL_GOALS:
            run = await run_structural(case)
            stats = summary.stats(case.cls)
            stats.attempts += 1
            stats.successes += int(run.result.achieved)
    fp = [floor_verdict(c).refused for c in benign]
    by_class: dict[str, list[bool]] = {}
    for case in attacks:
        by_class.setdefault(case.cls, []).append(blocked[case.id])
    summary.floor = {
        "block_rate_by_class": {
            k: wilson(sum(v), len(v)).to_dict() for k, v in sorted(by_class.items())
        },
        "false_positive_rate": wilson(sum(fp), len(fp)).to_dict(),
        "note": "L1 plus heuristics only; the guard model is not exercised offline",
    }
    summary.finished_at = datetime.now(UTC).isoformat()
    return summary


# -------------------------------------------------------------------------------- live --
async def _similar(candidate: Case, existing: list[Case], embedder: Embedder) -> bool:
    texts = [" ".join(c.turns) + " " + c.poison for c in existing]
    if not texts:
        return False
    target = (
        await embedder.embed([" ".join(candidate.turns) + " " + candidate.poison], "document")
    )[0]
    vectors = await embedder.embed(texts, "document")
    return any(cosine(target, v) >= DEDUPE_SIMILARITY for v in vectors)


async def minimise(case: Case, factory: TargetFactory, judge: Judge | None) -> Case:
    """Shrink a successful multi-turn attack to its last turn if that alone still succeeds."""
    if len(case.turns) < 2:
        return case
    shorter = case.model_copy(update={"turns": case.turns[-1:]})
    result = await run_attempt(shorter, factory, judge)
    return shorter if result.achieved else case


async def promote(
    case: Case,
    factory: TargetFactory,
    judge: Judge | None,
    embedder: Embedder,
    *,
    path: Path = PROMOTED_PATH,
) -> bool:
    """Minimise, deduplicate against the corpus, and append. Returns whether it was added."""
    small = await minimise(case, factory, judge)
    existing = [*load_all(path)]
    if any(c.id == small.id for c in existing) or await _similar(small, existing, embedder):
        return False
    append(path, [small.model_copy(update={"source": "promoted"})])
    return True


async def run_live(
    settings: Settings,
    factory: TargetFactory,
    attacker: Attacker,
    judge: Judge | None,
    *,
    classes: list[str] | None = None,
    attempts_per_class: int | None = None,
    rounds: int | None = None,
    embedder: Embedder | None = None,
    promoted_path: Path = PROMOTED_PATH,
    attacker_tokens: Callable[[], Awaitable[tuple[int, int]]] | None = None,
) -> Summary:
    """Generate, play, refine, and promote until every class is done or the budget is spent."""
    embedder = embedder or HashingEmbedder()
    wanted = classes or list(CLASS_BRIEFS)
    per_class = attempts_per_class or settings.redteam_attempts_per_class
    max_rounds = settings.redteam_max_refinement_rounds if rounds is None else rounds
    meter = Meter(settings)
    summary = Summary("live", datetime.now(UTC).isoformat())

    async def account() -> None:
        if attacker_tokens is not None:
            used_in, used_out = await attacker_tokens()
            meter.tokens_in, meter.tokens_out = (
                used_in + meter.tokens_in,
                used_out + meter.tokens_out,
            )

    async def play(case: Case) -> AttemptResult:
        result = await run_attempt(case, factory, judge)
        meter.add(result.tokens_in, result.tokens_out)
        return result

    for cls in wanted:
        stats = summary.stats(cls)
        tried: list[str] = []
        while stats.attempts < per_class:
            if meter.exhausted:
                summary.stopped_for_budget = True
                break
            batch = await attacker.generate(cls, min(5, per_class - stats.attempts), tried)
            if not batch:
                break
            for case in batch:
                if meter.exhausted or stats.attempts >= per_class:
                    break
                tried.append(case.turns[0])
                result = await play(case)
                stats.attempts += 1
                current, round_no = case, 0
                while not result.achieved and round_no < max_rounds and not meter.exhausted:
                    refined = await attacker.refine(current, result.reply)
                    if refined is None:
                        break
                    round_no += 1
                    current, result = refined, await play(refined)
                if result.achieved:
                    stats.successes += 1
                    stats.refined_successes += int(round_no > 0)
                    if await promote(current, factory, judge, embedder, path=promoted_path):
                        summary.promoted.append(current.id)
        if summary.stopped_for_budget:
            break
    summary.spend_usd = meter.usd
    summary.finished_at = datetime.now(UTC).isoformat()
    return summary


# ----------------------------------------------------------------------------- reporting --
def write_summary(summary: Summary, directory: Path = RUNS_DIR) -> Path:
    """Write ``<date>-<mode>.json`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = summary.started_at[:10]
    path = directory / f"{stamp}-{summary.mode}.json"
    path.write_text(json.dumps(summary.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


async def store_summary(settings: Settings, summary: Summary) -> str:
    """Insert the summary into the application's ``redteam_runs`` table (Settings reads it)."""
    db = Database(settings.sqlite_file)
    await db.connect()
    try:
        run_id = new_id("rt")
        await db.execute(
            "INSERT INTO redteam_runs(id, started_at, finished_at, summary_json) VALUES (?,?,?,?)",
            (run_id, summary.started_at, summary.finished_at, to_json(summary.to_dict())),
        )
        return run_id
    finally:
        await db.close()


def trend(directory: Path = RUNS_DIR, days: int = 14) -> list[dict[str, Any]]:
    """The last ``days`` live summaries, oldest first: date, attempts, successes."""
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*-live.json"))[-days:]:
        data = json.loads(path.read_text("utf-8"))
        rows.append(
            {"date": path.name[:10], "attempts": data["attempts"], "successes": data["successes"]}
        )
    return rows


def markdown_table(summary: dict[str, Any]) -> str:
    """A per-class table for the README or the weekly summary."""
    lines = ["| Class | Attempts | Successes | Success rate (95% CI) |", "|---|---|---|---|"]
    for name, row in summary["by_class"].items():
        rate = row["success_rate"]
        lines.append(
            f"| {name} | {row['attempts']} | {row['successes']} | "
            f"{rate['rate']:.1%} ({rate['ci_low']:.1%} to {rate['ci_high']:.1%}) |"
        )
    return "\n".join(lines)


def publish(ids: list[str], source: Path = PROMOTED_PATH, target: Path = PUBLISHED_PATH) -> int:
    """Copy human-reviewed promoted cases into the tracked corpus (the "human glance" step)."""
    pending = {c.id: c for c in load(source)}
    chosen = [pending[i] for i in ids if i in pending]
    done = {c.id for c in load(target)}
    return append(target, [c for c in chosen if c.id not in done])


# ------------------------------------------------------------------------------------ CLI --
def _live_parts(
    settings: Settings, out_dir: Path
) -> tuple[TargetFactory, Attacker, Judge, Database]:
    from src.core.wiring import build_providers

    providers = build_providers(settings)
    control = Database(out_dir / "nightly-control.db")
    router = LLMRouter(settings, providers, control)

    async def factory(workdir: Path, backend: RecordingBackend) -> Any:
        return await build_target(workdir, None, backend, providers=providers, base=settings)

    return factory, Attacker(router), Judge(router), control


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.publish:
        count = publish(args.publish)
        print(f"published {count} case(s) to {PUBLISHED_PATH}")
        return 0
    if args.report:
        print(markdown_table(json.loads(Path(args.report).read_text("utf-8"))))
        return 0
    if args.live:
        out_dir = Path("data/redteam")
        out_dir.mkdir(parents=True, exist_ok=True)
        factory, attacker, judge, control = _live_parts(settings, out_dir)
        await control.connect()

        async def tokens() -> tuple[int, int]:
            row = await control.fetchone(
                "SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o "
                "FROM usage_log"
            )
            return (int(row["i"]), int(row["o"])) if row else (0, 0)

        try:
            summary = await run_live(
                settings,
                factory,
                attacker,
                judge,
                classes=args.classes or None,
                attempts_per_class=args.attempts,
                rounds=args.rounds,
                attacker_tokens=tokens,
            )
        finally:
            await control.close()
    else:
        summary = await run_offline()
    path = write_summary(summary)
    await store_summary(settings, summary)
    print(markdown_table(summary.to_dict()))
    print(f"\nwrote {path}")
    return (
        1
        if (summary.mode == "offline" and any(c.successes for c in summary.classes.values()))
        else 0
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="generate attacks with real models")
    mode.add_argument("--offline", action="store_true", help="replay the corpus (default)")
    parser.add_argument("--classes", nargs="*", help="limit to these attack classes")
    parser.add_argument("--attempts", type=int, help="attempts per class")
    parser.add_argument("--rounds", type=int, help="refinement rounds")
    parser.add_argument("--publish", nargs="*", help="copy reviewed promoted case ids to the repo")
    parser.add_argument("--report", help="print a markdown table from a summary file")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
