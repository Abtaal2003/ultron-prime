"""Routing and running a plan (spec 4.11, "how it coexists with the normal graph").

``CAMEL_MODE`` decides whether the CaMeL path is used at all. In ``egress_turns`` mode a turn
takes it only when its plan can change something or send something out *and* touches untrusted
or private data; plain conversation, code help and read-only lookups stay on the normal graph, so
the persona and the latency do not pay for protection they do not need.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.core.camel.capabilities import TOOL_SOURCES
from src.core.camel.interpreter import (
    Confirm,
    Interpreter,
    Limits,
    Parser,
    RunResult,
    ToolPort,
)
from src.core.camel.planner import Plan
from src.core.camel.policies import CapabilityPolicy

Mode = Literal["off", "egress_turns", "always"]
SINK_TOOLS = frozenset(
    {
        "web_omniscience_search",
        "network_ping_diagnostic",
        "file_system_scribe",
        "artifact_forge",
        "propose_memory",
        "isolated_python_sandbox",
    }
)
"""Tools that send something out or change something, besides any tool flagged ``is_egress``."""
SOURCE_TOOLS = frozenset(TOOL_SOURCES)
MAX_NARRATION_CHARS = 6000


@dataclass(slots=True)
class Route:
    """Whether a turn takes the CaMeL path, and why."""

    use_camel: bool
    reason: str


def route_turn(
    mode: Mode,
    plan: Plan | None,
    *,
    tainted: bool,
    extra_sinks: Collection[str] = (),
) -> Route:
    """Decide the path for one turn from the mode and the (already validated) plan."""
    if mode == "off":
        return Route(False, "CAMEL_MODE is off")
    if plan is None:
        return Route(False, "no usable plan")
    if plan.info.empty:
        return Route(False, "the plan calls no tools")
    if mode == "always":
        return Route(True, "CAMEL_MODE=always")
    tools = set(plan.info.tools)
    sinks = SINK_TOOLS | set(extra_sinks)
    if not tools & sinks:
        return Route(False, "the plan cannot send or change anything")
    if tainted or tools & SOURCE_TOOLS or plan.info.quarantined_reads:
        return Route(True, "the plan can send or change something and touches untrusted data")
    return Route(False, "the plan touches only the operator's own words")


async def execute(
    plan: Plan,
    *,
    tools: Collection[str],
    port: ToolPort,
    operator_text: str,
    policy: CapabilityPolicy,
    parser: Parser | None,
    confirm: Confirm | None,
    limits: Limits,
    strict: bool,
    tainted: bool,
) -> RunResult:
    """Run a validated plan through the interpreter."""
    interpreter = Interpreter(
        tools=frozenset(tools),
        port=port,
        policy=policy,
        operator_text=operator_text,
        parser=parser,
        confirm=confirm,
        limits=limits,
        strict=strict,
        tainted=tainted,
    )
    return await interpreter.run(plan.module)


def render_results(
    items: Sequence[tuple[str, Any]], aborted: str = "", limit: int = MAX_NARRATION_CHARS
) -> str:
    """The plan's results as plain text for the narrator (it wraps this as untrusted data)."""
    lines: list[str] = []
    for index, (label, data) in enumerate(items, 1):
        body = data if isinstance(data, str) else json.dumps(data, default=str)
        lines.append(f"{label or f'result {index}'}: {body}")
    if aborted:
        lines.append(f"The plan stopped early: {aborted}")
    if not lines:
        lines.append("The plan produced no results.")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"
