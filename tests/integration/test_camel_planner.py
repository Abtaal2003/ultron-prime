"""Planner, signatures and routing for the CaMeL path."""

from __future__ import annotations

import pytest
from src.config import Settings
from src.core.camel.engine import SINK_TOOLS, render_results, route_turn
from src.core.camel.interpreter import Limits, check_program
from src.core.camel.planner import (
    Plan,
    PlanError,
    Planner,
    extract_code,
    render_signatures,
    signature,
)
from src.core.llm.base import TextPart
from src.core.llm.fake import FakeProvider, Reply
from src.core.llm.router import LLMRouter
from src.tools.search import WebOmniscienceSearch

from .conftest import EchoTool, FakeBackend, RoleScript, routed

TOOLS = ["web_omniscience_search", "echo"]


def make_plan(source: str) -> Plan:
    module, info = check_program(source, set(TOOLS))
    return Plan(source, module, info)


def planner_for(settings: Settings, script: RoleScript) -> Planner:
    cfg = routed(settings)
    router = LLMRouter(cfg, {"fake": FakeProvider(script, name="fake")})
    return Planner(router, Limits())


def test_extract_code_takes_the_fenced_block_or_the_whole_reply() -> None:
    assert extract_code("sure\n```python\nx = 1\n```\nbye") == "x = 1"
    assert extract_code("x = 2") == "x = 2"


def test_signatures_are_typed_and_mark_optional_arguments() -> None:
    text = signature(WebOmniscienceSearch(backends=[FakeBackend()]))
    assert text.startswith("web_omniscience_search(query: str")
    assert "-> {query: str" in text
    assert "= ..." in text  # optional parameters are marked
    assert "echo(" in render_signatures([EchoTool()])


async def test_a_valid_first_reply_is_one_attempt(settings: Settings, script: RoleScript) -> None:
    script.push("planner", Reply(text='```python\necho(text="hi")\n```'))
    plan = await planner_for(settings, script).plan("echo hi", [EchoTool()])
    assert plan.attempts == 1 and plan.info.tools == ["echo"]
    assert plan.usage.input_tokens > 0


async def test_context_lines_reach_the_planner(settings: Settings, script: RoleScript) -> None:
    script.push("planner", Reply(text="pass"))
    await planner_for(settings, script).plan(
        "and again",
        [EchoTool()],
        prior_operator=["first ask"],
        attachments_note="Attachment: notes.txt (text, 12 bytes)",
    )
    text = "".join(
        p.text
        for m in script.requests("planner")[0].messages
        for p in m.parts
        if isinstance(p, TextPart)
    )
    assert "- first ask" in text and "notes.txt" in text and "and again" in text


async def test_repair_feeds_the_error_back_then_succeeds(
    settings: Settings, script: RoleScript
) -> None:
    script.push("planner", Reply(text="import os"), Reply(text="pass"))
    plan = await planner_for(settings, script).plan("x", [EchoTool()])
    assert plan.attempts == 2


async def test_planning_gives_up_after_the_repair_budget(
    settings: Settings, script: RoleScript
) -> None:
    script.push("planner", *[Reply(text="import os")] * 5)
    with pytest.raises(PlanError):
        await planner_for(settings, script).plan("x", [EchoTool()])
    assert len(script.requests("planner")) == 3


def test_routing_by_mode() -> None:
    sink = make_plan('web_omniscience_search(query="a")')
    reader = make_plan('echo(text="a")')
    empty = make_plan("pass")
    assert not route_turn("off", sink, tainted=True).use_camel
    assert not route_turn("always", None, tainted=True).use_camel
    assert not route_turn("always", empty, tainted=True).use_camel
    assert route_turn("always", reader, tainted=False).use_camel
    assert route_turn("egress_turns", sink, tainted=False).use_camel  # a source tool is a sink
    assert not route_turn("egress_turns", reader, tainted=True).use_camel  # no sink in the plan
    assert route_turn("egress_turns", reader, tainted=True, extra_sinks={"echo"}).use_camel
    assert not route_turn("egress_turns", reader, tainted=False, extra_sinks={"echo"}).use_camel
    assert "file_system_scribe" in SINK_TOOLS


def test_render_results_labels_truncates_and_reports_an_abort() -> None:
    text = render_results([("hits", {"n": 1}), ("", "plain")], aborted="tool failed")
    assert 'hits: {"n": 1}' in text and "result 2: plain" in text
    assert text.endswith("The plan stopped early: tool failed")
    assert render_results([]) == "The plan produced no results."
    long = render_results([("a", "x" * 100)], limit=20)
    assert len(long) == 20 and long.endswith("…")
