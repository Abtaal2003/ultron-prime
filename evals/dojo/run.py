"""Run an AgentDojo suite against Ultron's structural defences and report with intervals."""

from __future__ import annotations

import argparse
import json
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.basic_elements import InitQuery
from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop, ToolsExecutor
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.task_suite.task_suite import TaskSuite

from evals.dojo.gate import EgressGate, L1Redact, ScrubHeld
from evals.dojo.scripted import GullibleLLM
from evals.stats import Rate, wilson

BENCHMARK_VERSION = "v1.2.1"
DEFAULT_SUITES = ("banking", "slack", "travel", "workspace", "ultron")
DEFAULT_ATTACKS = ("important_instructions", "direct")
CONFIGS = ("none", "l1", "egress", "full")
OUT_DIR = Path("data/evals")

ConfigFactory = Callable[[], list[BasePipelineElement]]
CONFIG_ELEMENTS: dict[str, ConfigFactory] = {
    "none": lambda: [ToolsExecutor()],
    "l1": lambda: [ToolsExecutor(), L1Redact()],
    "egress": lambda: [EgressGate()],
    "full": lambda: [EgressGate(), L1Redact()],
}


def build_pipeline(config: str, llm: BasePipelineElement) -> AgentPipeline:
    """Wrap ``llm`` in the tool loop for one defence configuration."""
    pipeline = AgentPipeline(
        [InitQuery(), llm, ToolsExecutionLoop([*CONFIG_ELEMENTS[config](), llm]), ScrubHeld()]
    )
    pipeline.name = llm.name
    return pipeline


@dataclass(slots=True)
class Cell:
    """Counts for one suite, attack and configuration."""

    pairs: int = 0
    attacks_succeeded: int = 0
    utility_under_attack: int = 0
    benign_tasks: int = 0
    benign_utility: int = 0
    seconds: float = 0.0

    def asr(self) -> Rate:
        """Attack success rate with its interval."""
        return wilson(self.attacks_succeeded, self.pairs)

    def utility_attack(self) -> Rate:
        """Utility under attack."""
        return wilson(self.utility_under_attack, self.pairs)

    def utility_clean(self) -> Rate:
        """Utility with no attack."""
        return wilson(self.benign_utility, self.benign_tasks)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "pairs": self.pairs,
            "asr": self.asr().to_dict(),
            "utility_under_attack": self.utility_attack().to_dict(),
            "utility_benign": self.utility_clean().to_dict(),
            "seconds": round(self.seconds, 1),
        }


@dataclass(slots=True)
class Report:
    """All cells of a run."""

    version: str = BENCHMARK_VERSION
    cells: dict[str, Cell] = field(default_factory=dict)
    dead_tasks: dict[str, int] = field(default_factory=dict)
    unattackable: dict[str, int] = field(default_factory=dict)

    def key(self, suite: str, attack: str, config: str) -> str:
        """The cell key."""
        return f"{suite}/{attack}/{config}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "benchmark": f"agentdojo {self.version}",
            "model": "scripted gullible (obeys every injection it reads)",
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "injection_tasks_excluded": self.dead_tasks,
            "pairs_unattackable_even_without_defences": self.unattackable,
            "cells": {k: c.to_dict() for k, c in self.cells.items()},
        }


def load(suite_name: str, version: str = BENCHMARK_VERSION) -> TaskSuite[Any]:
    """Load a built-in suite, or UltronDojo."""
    if suite_name == "ultron":
        from evals.dojo.ultron_suite import task_suite

        return task_suite
    return get_suite(version, suite_name)


Injections = dict[str, dict[str, dict[str, str]]]


def live_injection_tasks(suite: TaskSuite[Any]) -> list[str]:
    """Injection tasks whose own ground truth satisfies their security oracle.

    A task that fails this check could never register as a success, which would flatter every
    defence, so it is left out and counted in the report.
    """
    user_task = next(iter(suite.user_tasks.values()))
    live: list[str] = []
    for inj_id, inj_task in suite.injection_tasks.items():
        env = suite.load_and_inject_default_environment({})
        _, security = suite.run_task_with_pipeline(
            GroundTruthPipeline(inj_task), user_task, inj_task, {}, environment=env
        )
        if security:
            live.append(inj_id)
    return live


def generate_injections(suite: TaskSuite[Any], attack_name: str, inj_ids: list[str]) -> Injections:
    """Build the attack text for every injectable user task and live injection task.

    AgentDojo's own ``generate_injections`` first calls ``suite.check()``, which in 0.1.35 reports
    every task as not injectable (it tests ``isinstance(content, str)`` but content is a list of
    blocks), so the pairs are generated directly here.
    """
    probe = build_pipeline("none", GullibleLLM(next(iter(suite.user_tasks.values()))))
    attack = load_attack(attack_name, suite, probe)
    out: Injections = {}
    for user_id, user_task in suite.user_tasks.items():
        for inj_id in inj_ids:
            try:
                out.setdefault(user_id, {})[inj_id] = attack.attack(
                    user_task, suite.injection_tasks[inj_id]
                )
            except ValueError:  # the user task never shows the model an injection vector
                continue
    return out


def attackable(suite: TaskSuite[Any], injections: Injections) -> tuple[Injections, int]:
    """Keep only the pairs the undefended gullible model loses.

    Some pairs can never succeed (the injection vector is not shown for that user task, or the
    oracle needs state the user task does not create). Counting them would credit every defence
    with blocking an attack that was never possible, so they are dropped and counted.
    """
    kept: Injections = {}
    dropped = 0
    for user_id, pairs in injections.items():
        user_task = suite.user_tasks[user_id]
        for inj_id, vectors in pairs.items():
            inj_task = suite.injection_tasks[inj_id]
            pipeline = build_pipeline("none", GullibleLLM(user_task, inj_task))
            _, security = suite.run_task_with_pipeline(pipeline, user_task, inj_task, vectors)
            if security:
                kept.setdefault(user_id, {})[inj_id] = vectors
            else:
                dropped += 1
    return kept, dropped


def run_cell(
    suite: TaskSuite[Any],
    attack_name: str,
    config: str,
    *,
    limit: int | None = None,
    injections: Injections | None = None,
) -> Cell:
    """Evaluate every user and injection task pair (and every clean user task) once."""
    cell = Cell()
    started = time.monotonic()
    if injections is None:
        injections = generate_injections(suite, attack_name, live_injection_tasks(suite))
    user_tasks = list(suite.user_tasks.items())[:limit]
    for user_id, user_task in user_tasks:
        pipeline = build_pipeline(config, GullibleLLM(user_task))
        clean_utility, _ = suite.run_task_with_pipeline(pipeline, user_task, None, {})
        cell.benign_tasks += 1
        cell.benign_utility += int(clean_utility)
        for inj_id, vectors in injections.get(user_id, {}).items():
            inj_task = suite.injection_tasks[inj_id]
            attacked = build_pipeline(config, GullibleLLM(user_task, inj_task))
            utility, security = suite.run_task_with_pipeline(attacked, user_task, inj_task, vectors)
            cell.pairs += 1
            cell.attacks_succeeded += int(security)
            cell.utility_under_attack += int(utility)
    cell.seconds = time.monotonic() - started
    return cell


def run(
    suites: tuple[str, ...] = DEFAULT_SUITES,
    attacks: tuple[str, ...] = DEFAULT_ATTACKS,
    configs: tuple[str, ...] = CONFIGS,
    *,
    limit: int | None = None,
) -> Report:
    """Run the matrix. Injections are generated once per suite and attack."""
    report = Report()
    for suite_name in suites:
        suite = load(suite_name)
        live = live_injection_tasks(suite)
        report.dead_tasks[suite_name] = len(suite.injection_tasks) - len(live)
        for attack in attacks:
            generated, dropped = attackable(suite, generate_injections(suite, attack, live))
            report.unattackable[f"{suite_name}/{attack}"] = dropped
            for config in configs:
                report.cells[report.key(suite_name, attack, config)] = run_cell(
                    suite, attack, config, limit=limit, injections=generated
                )
    return report


def markdown(report: Report) -> str:
    """Render the report as a table."""
    lines = [
        "| Suite | Attack | Defence | ASR (95% CI) | Utility under attack | Utility, no attack |",
        "|---|---|---|---|---|---|",
    ]
    for key, cell in report.cells.items():
        suite, attack, config = key.split("/")
        lines.append(
            f"| {suite} | {attack} | {config} | {cell.asr()} | "
            f"{cell.utility_attack().value:.1%} | {cell.utility_clean().value:.1%} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m evals.dojo.run``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suites", nargs="*", default=list(DEFAULT_SUITES))
    parser.add_argument("--attacks", nargs="*", default=list(DEFAULT_ATTACKS))
    parser.add_argument("--configs", nargs="*", default=list(CONFIGS))
    parser.add_argument("--limit", type=int, default=None, help="user tasks per suite")
    parser.add_argument("--write", action="store_true", help="store JSON and a markdown table")
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    report = run(tuple(args.suites), tuple(args.attacks), tuple(args.configs), limit=args.limit)
    print(markdown(report))
    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "agentdojo.json").write_text(json.dumps(report.to_dict(), indent=2))
        (OUT_DIR / "agentdojo.md").write_text(markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
