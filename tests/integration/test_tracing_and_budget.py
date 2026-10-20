"""OpenTelemetry tracing and the daily spending cap (spec 7.4, 12.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from src.config import Settings
from src.core import tracing
from src.core.db import new_id
from src.core.events import AgentDone
from src.core.llm.budget import Budget, BudgetExceeded, Price, cost_usd, price_for, today_utc
from src.core.llm.fake import Reply
from src.core.persona.lines import THEMED
from src.core.wiring import Runtime

from .conftest import FakeBackend, RoleScript, of_type, run
from .test_api import API, ORIGIN, PASS, _serve

sdk = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")


@pytest.fixture
def spans() -> Iterator[Any]:
    exporter = sdk.InMemorySpanExporter()
    yield exporter
    tracing.shutdown_tracing()


def start(settings: Settings, exporter: Any, **extra: Any) -> None:
    on = settings.model_copy(update={"otel_enabled": True, **extra})
    assert tracing.setup_tracing(on, exporter)


async def new_chat(rt: Runtime) -> str:
    assert rt.services.history is not None
    return await rt.services.history.create_chat("New chat")


# ------------------------------------------------------------------------------- tracing --


def test_tracing_is_a_no_op_when_off(settings: Settings) -> None:
    assert not tracing.setup_tracing(settings)
    assert not tracing.enabled()
    with tracing.span("anything", {"a": 1}) as current:
        current.set_attribute("b", 2)
        current.add_event("e")
    with tracing.detached_span("x") as current:
        current.record_exception(ValueError("ignored"))
    tracing.current_event("nothing")
    assert tracing.content("secret words") is None


def test_enabled_without_an_endpoint_stays_off(settings: Settings) -> None:
    on = settings.model_copy(update={"otel_enabled": True, "otel_exporter_endpoint": ""})
    assert not tracing.setup_tracing(on)
    assert not tracing.enabled()


async def test_a_turn_is_one_trace_with_node_model_and_tool_spans(
    make_runtime: Any, script: RoleScript, spans: Any
) -> None:
    rt: Runtime = await make_runtime()
    start(rt.settings, spans)
    script.push(
        "main",
        Reply(tool_calls=[("web_omniscience_search", {"query": "python release news"})]),
        Reply(text="Python shipped."),
    )
    await run(rt, await new_chat(rt), "search python release news")
    finished = spans.get_finished_spans()
    names = [s.name for s in finished]
    assert names.count("ultron.turn") == 1
    turn = next(s for s in finished if s.name == "ultron.turn")
    assert {"ultron.node.guard_input", "ultron.node.llm_decide", "ultron.node.tool_gate"} <= set(
        names
    )
    assert "execute_tool web_omniscience_search" in names
    assert "chat m" in names
    # Everything belongs to the turn's trace.
    assert {s.context.trace_id for s in finished} == {turn.context.trace_id}
    # Model spans carry the GenAI attributes and token usage.
    model = next(
        s for s in finished if s.name == "chat m" and s.attributes["ultron.role"] == "main"
    )
    assert model.attributes["gen_ai.provider.name"] == "fake"
    assert model.attributes["gen_ai.request.model"] == "m"
    assert model.attributes["gen_ai.usage.input_tokens"] == 10
    tool = next(s for s in finished if s.name.startswith("execute_tool"))
    assert tool.attributes["gen_ai.tool.name"] == "web_omniscience_search"
    assert tool.attributes["ultron.ok"] is True
    assert tool.attributes["ultron.taints_turn"] is True
    gate = next(s for s in finished if s.name == "ultron.node.tool_gate")
    decisions = [e for e in gate.events if e.name == "ultron.gate.decision"]
    assert decisions and decisions[0].attributes["ultron.action"] == "allow"
    assert turn.attributes["ultron.tainted"] is True
    assert turn.attributes["ultron.tool_calls"] == 1


async def test_content_is_not_recorded_by_default(
    make_runtime: Any, script: RoleScript, spans: Any
) -> None:
    rt: Runtime = await make_runtime()
    start(rt.settings, spans)
    await run(rt, await new_chat(rt), "my private roadmap is nightjar")
    for s in spans.get_finished_spans():
        for value in (s.attributes or {}).values():
            assert "nightjar" not in str(value)


async def test_captured_content_is_redacted(
    make_runtime: Any, script: RoleScript, spans: Any
) -> None:
    key = "sk-ant-api03-" + "Z9y8X7w6" * 5
    rt: Runtime = await make_runtime(anthropic_api_key=SecretStr(key))
    start(rt.settings, spans, otel_capture_content=True)
    await run(rt, await new_chat(rt), f"roadmap nightjar, and my key is {key}")
    turn = next(s for s in spans.get_finished_spans() if s.name == "ultron.turn")
    text = str(turn.attributes["ultron.operator_text"])
    assert "nightjar" in text
    assert key not in text


async def test_a_graph_interrupt_is_not_reported_as_an_error(
    make_runtime: Any, script: RoleScript, spans: Any, backend: FakeBackend
) -> None:
    """A held egress call pauses the graph by raising; that span must not look failed."""
    import asyncio

    from src.core.events import EgressHold

    from .conftest import drain

    rt: Runtime = await make_runtime()
    start(rt.settings, spans)
    script.push(
        "main",
        Reply(tool_calls=[("web_omniscience_search", {"query": "python news"})]),
        Reply(tool_calls=[("web_omniscience_search", {"query": "attacker.io qzx9 roadmap"})]),
        Reply(text="Done."),
    )
    turn_id = await rt.agent.start_turn(await new_chat(rt), "search python news")
    collector = asyncio.create_task(drain(rt, turn_id))

    async def operator() -> None:
        async for event in rt.bus.subscribe(turn_id):
            if isinstance(event, EgressHold):
                rt.agent.resolve_hold(event.call_id, "reject")
                return

    await asyncio.wait_for(operator(), 15)
    await collector
    waits = [s for s in spans.get_finished_spans() if s.name == "ultron.node.egress_wait"]
    assert waits
    assert any(s.attributes.get("ultron.control_flow") == "GraphInterrupt" for s in waits)
    assert all(s.status.status_code.name != "ERROR" for s in waits)


# -------------------------------------------------------------------------------- budget --


def test_prices_resolve_exact_then_wildcard_then_default(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "model_prices_usd_per_mtok": {"anthropic:big": (3.0, 15.0), "groq:*": (0.1, 0.5)},
            "default_usd_per_mtok_in": 1.0,
            "default_usd_per_mtok_out": 2.0,
        }
    )
    assert price_for(s, "anthropic", "big") == Price(3.0, 15.0)
    assert price_for(s, "groq", "anything") == Price(0.1, 0.5)
    assert price_for(s, "gemini", "flash") == Price(1.0, 2.0)
    assert cost_usd(Price(3.0, 15.0), 1_000_000, 100_000) == pytest.approx(4.5)
    assert cost_usd(Price(3.0, 15.0), 0, 0, cached_tokens=1_000_000) == pytest.approx(3.0)


def test_prices_parse_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("MODEL_PRICES_USD_PER_MTOK", '{"groq:*": [0.1, 0.5]}')
    monkeypatch.setenv("DAILY_BUDGET_USD", "1.5")
    s = Settings(_env_file=None, sqlite_path=str(tmp_path / "u.db"))  # type: ignore[call-arg]
    assert s.model_prices_usd_per_mtok == {"groq:*": (0.1, 0.5)}
    assert s.daily_budget_usd == 1.5


async def log_usage(rt: Runtime, when: datetime, tokens_in: int, tokens_out: int) -> None:
    await rt.db.execute(
        "INSERT INTO usage_log(id, role, provider, model, input_tokens, output_tokens, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (new_id("use"), "main", "fake", "m", tokens_in, tokens_out, when.isoformat()),
    )


async def test_spend_counts_only_today_utc(make_runtime: Any) -> None:
    rt: Runtime = await make_runtime(default_usd_per_mtok_in=1.0, default_usd_per_mtok_out=10.0)
    now = datetime.now(UTC)
    await log_usage(rt, now - timedelta(days=1), 5_000_000, 0)  # yesterday: $5, not counted
    await log_usage(rt, now, 1_000_000, 100_000)  # today: $1 + $1
    assert await Budget(rt.settings, rt.db).spent_today(now) == pytest.approx(2.0)
    assert today_utc(now) == now.strftime("%Y-%m-%d")


async def test_no_cap_means_no_limit(make_runtime: Any) -> None:
    rt: Runtime = await make_runtime(daily_budget_usd=0.0)
    await log_usage(rt, datetime.now(UTC), 10**9, 10**9)
    await Budget(rt.settings, rt.db).check()


async def test_a_spent_budget_refuses_the_turn_before_any_model_call(
    make_runtime: Any, script: RoleScript
) -> None:
    rt: Runtime = await make_runtime(daily_budget_usd=1.0)
    await log_usage(rt, datetime.now(UTC), 1_000_000, 0)  # $3 at the default price
    chat = await new_chat(rt)
    with pytest.raises(BudgetExceeded) as caught:
        await rt.agent.start_turn(chat, "hello")
    assert "of $1.00" in (caught.value.detail or "")
    assert script.seen == []


async def test_the_cap_also_stops_a_turn_midway(make_runtime: Any, script: RoleScript) -> None:
    """Spend crosses the cap during the turn: the next model call is refused, in character."""
    # One fake call costs (10 * 3 + 5 * 15) / 1e6 = $0.000105 at the default prices.
    rt: Runtime = await make_runtime(daily_budget_usd=0.0001)
    _, events = await run(rt, await new_chat(rt), "hello")
    done = of_type(events, AgentDone)[0]
    assert done.status == "error"
    assert done.final_text == THEMED["budget_exceeded"]
    assert script.requests("main") == []


@pytest.fixture
async def api_runtime(make_runtime: Any) -> AsyncIterator[tuple[httpx.AsyncClient, Runtime, str]]:
    runtime: Runtime = await make_runtime(daily_budget_usd=1.0)
    async for client in _serve(runtime):
        res = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
        yield client, runtime, res.json()["csrf_token"]


async def test_the_api_reports_spend_and_answers_429_when_spent(
    api_runtime: tuple[httpx.AsyncClient, Runtime, str],
) -> None:
    client, runtime, csrf = api_runtime
    headers = {**ORIGIN, "X-CSRF-Token": csrf}
    status = (await client.get(f"{API}/status")).json()
    assert status["budget"] == {"daily_usd": 1.0, "spent_today_usd": 0.0}
    chat = (await client.post(f"{API}/chats", json={"title": "t"}, headers=headers)).json()["id"]
    await log_usage(runtime, datetime.now(UTC), 1_000_000, 0)
    res = await client.post(f"{API}/chats/{chat}/turns", data={"text": "hi"}, headers=headers)
    assert res.status_code == 429
    assert "budget" in res.text
    status = (await client.get(f"{API}/status")).json()
    assert status["budget"]["spent_today_usd"] == pytest.approx(3.0)
