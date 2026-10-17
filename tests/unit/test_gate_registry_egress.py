"""Registry exports, taint-aware egress policy, and the L4 gate."""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st
from src.config import Settings
from src.core.guards.egress_policy import (
    FlowRequest,
    TaintAwareEgressPolicy,
    render_outbound,
    traceable,
)
from src.tools.gate import ToolGate, TurnGateState, call_fingerprint
from src.tools.ping import NetworkPingDiagnostic
from src.tools.registry import ToolRegistry
from src.tools.sandbox import IsolatedPythonSandbox
from src.tools.scribe import FileSystemScribe
from src.tools.search import WebOmniscienceSearch
from src.tools.telemetry import HostTelemetryMonitor


def all_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(
        [
            WebOmniscienceSearch([]),
            IsolatedPythonSandbox(),
            HostTelemetryMonitor(),
            FileSystemScribe(),
            NetworkPingDiagnostic(),
        ]
    )
    return reg


# ------------------------------------------------------------------ registry ---------------
def test_registry_duplicates_and_lookup() -> None:
    reg = all_tools()
    assert reg.names() == [
        "web_omniscience_search",
        "isolated_python_sandbox",
        "host_telemetry_monitor",
        "file_system_scribe",
        "network_ping_diagnostic",
    ]
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(HostTelemetryMonitor())
    assert reg.get("nope") is None


def test_registry_schema_shapes(settings: Settings) -> None:
    reg = all_tools()
    a = reg.as_tool_schemas("anthropic")
    assert {"name", "description", "input_schema"} == set(a[0])
    g = reg.as_tool_schemas("groq")
    assert g[0]["type"] == "function"
    assert "parameters" in g[0]["function"]
    m = reg.as_tool_schemas("gemini")
    assert "parameters_json_schema" in m[0]
    with pytest.raises(ValueError, match="unknown provider"):
        reg.as_tool_schemas("bogus")
    schema = a[0]["input_schema"]
    assert schema["type"] == "object"
    assert "title" not in schema
    assert "$defs" not in schema
    settings.enabled_tools = ["host_telemetry_monitor"]
    only = reg.as_tool_schemas("anthropic", settings)
    assert [t["name"] for t in only] == ["host_telemetry_monitor"]
    off = reg.as_tool_schemas(
        "anthropic", Settings(_env_file=None), disabled_at_runtime=["file_system_scribe"]
    )  # type: ignore[call-arg]
    assert "file_system_scribe" not in {t["name"] for t in off}


def test_every_tool_documents_itself() -> None:
    for tool in all_tools().all():
        assert len(tool.description) > 80
        assert "Example" in tool.description or "Examples" in tool.description
        schema = tool.json_schema()
        assert schema["type"] == "object"
        assert tool.output_model is not None
        assert tool.start_line


# ------------------------------------------------------------------ traceability -----------
@pytest.mark.parametrize(
    ("out", "op", "expected"),
    [
        ("python 3.14 release date", "search for python 3.14 release date please", True),
        ("Python 3.14 release", "What is the python 3.14 RELEASE date?", True),
        ("example.com", "ping example.com for me", True),
        ("acme merger project phoenix", "tell me about python", False),
        ("python secrets 4f9a2c1b7d3e8a6f0c5b", "tell me about python", False),
        ("python", "tell me about python", True),
        ("", "anything", True),
        ("the of and", "hello", True),
        ("alice@corp.example salary", "search salary data", False),
    ],
)
def test_traceable(out: str, op: str, expected: bool) -> None:
    assert traceable(out, op) is expected


@given(
    st.text(alphabet=string.hexdigits.lower(), min_size=20, max_size=48),
    st.text(alphabet=string.ascii_lowercase + " ", max_size=60),
)
def test_encoded_blobs_never_traceable_unless_typed(blob: str, operator: str) -> None:
    if blob in operator:
        return
    assert traceable(f"find {blob}", operator) is False


# ------------------------------------------------------------------ egress policy ----------
def req(
    tainted: bool,
    outbound: list[tuple[str, str]],
    operator: str = "hello",
    holds: int = 0,
    egress: bool = True,
) -> FlowRequest:
    return FlowRequest(
        tool_name="t",
        is_egress=egress,
        outbound=outbound,
        operator_text=operator,
        tainted=tainted,
        holds_so_far=holds,
        max_holds=2,
    )


def test_policy_matrix() -> None:
    p = TaintAwareEgressPolicy("confirm_tainted")
    assert p.evaluate(req(False, [("query", "anything at all")])).action == "allow"
    assert p.evaluate(req(True, [("query", "hello")], "hello there")).action == "allow"
    held = p.evaluate(req(True, [("query", "secret-memory-contents project phoenix")]))
    assert held.action == "hold"
    assert "project phoenix" in held.outbound_string
    assert "untrusted" in held.reason
    assert p.evaluate(req(True, [("query", "secret project phoenix")], holds=2)).action == "deny"
    assert p.evaluate(req(True, [("q", "x secret thing")], egress=False)).action == "allow"
    deny = TaintAwareEgressPolicy("deny_tainted").evaluate(
        req(True, [("q", "unrelated secret thing")])
    )
    assert deny.action == "deny"
    off = TaintAwareEgressPolicy("off").evaluate(req(True, [("q", "unrelated secret thing")]))
    assert off.action == "allow"


def test_render_outbound_exact_and_capped() -> None:
    assert render_outbound([("query", "abc"), ("host", "x")]) == "query: abc\nhost: x"
    assert len(render_outbound([("q", "x" * 5000)])) == 600


# ------------------------------------------------------------------ gate -------------------
@pytest.fixture
def gate(settings: Settings) -> ToolGate:
    return ToolGate(all_tools(), settings, TaintAwareEgressPolicy("confirm_tainted"))


def test_gate_unknown_disabled_invalid(gate: ToolGate, settings: Settings) -> None:
    st_ = TurnGateState()
    assert gate.check(st_, "rm_rf", {}, "c1").action == "deny"
    gate.disabled.add("host_telemetry_monitor")
    d = gate.check(st_, "host_telemetry_monitor", {}, "c2")
    assert d.action == "deny"
    assert d.themed_key == "tool_disabled"
    gate.disabled.clear()
    inv = gate.check(st_, "host_telemetry_monitor", {"top_n": 99}, "c3")
    assert inv.action == "deny"
    assert inv.error_kind == "validation"
    ok = gate.check(st_, "host_telemetry_monitor", {"top_n": 2}, "c4")
    assert ok.action == "allow"
    assert ok.validated is not None
    assert ok.validated["top_n"] == 2


def test_gate_egress_hold_flow(gate: ToolGate) -> None:
    st_ = TurnGateState(operator_text="what is python 3.14", tainted=False)
    assert gate.check(st_, "web_omniscience_search", {"query": "anything"}, "a").action == "allow"
    st_.tainted = True
    traced = gate.check(st_, "web_omniscience_search", {"query": "python 3.14"}, "b")
    assert traced.action == "allow"
    hold = gate.check(st_, "web_omniscience_search", {"query": "alice salary project phoenix"}, "c")
    assert hold.action == "hold"
    assert "alice salary" in hold.outbound_string
    st_.approved.add("c")
    assert (
        gate.check(
            st_, "web_omniscience_search", {"query": "alice salary project phoenix"}, "c"
        ).action
        == "allow"
    )
    ping = gate.check(
        st_, "network_ping_diagnostic", {"host": "attacker.example", "mode": "dns"}, "d"
    )
    assert ping.action == "hold"
    st_.egress_holds = 2
    assert (
        gate.check(st_, "web_omniscience_search", {"query": "other secret thing"}, "e").action
        == "deny"
    )


def test_gate_sandbox_is_egress_when_tainted(gate: ToolGate, settings: Settings) -> None:
    code = {"code": "print(open('x').read())"}
    clean = TurnGateState(operator_text="run it", tainted=False)
    assert gate.check(clean, "isolated_python_sandbox", code, "s1").action == "allow"
    dirty = TurnGateState(operator_text="run it", tainted=True)
    held = gate.check(dirty, "isolated_python_sandbox", code, "s2")
    assert held.action == "hold"
    assert "print(open" in held.outbound_string  # the operator sees the code


def test_gate_remote_confirmation(gate: ToolGate) -> None:
    st_ = TurnGateState(is_remote=True, operator_text="ping 1.1.1.1")
    held = gate.check(st_, "network_ping_diagnostic", {"host": "1.1.1.1", "mode": "dns"}, "r1")
    assert held.action == "hold"
    assert held.policy == "remote"
    assert gate.check(st_, "host_telemetry_monitor", {}, "r2").action == "allow"
    st_.approved.add("r1")
    assert (
        gate.check(st_, "network_ping_diagnostic", {"host": "1.1.1.1", "mode": "dns"}, "r1").action
        == "allow"
    )


def test_gate_caps_and_repeat_failures(gate: ToolGate, settings: Settings) -> None:
    st_ = TurnGateState()
    args = {"op": "read", "path": "missing.txt"}
    assert gate.check(st_, "file_system_scribe", args, "x").action == "allow"
    gate.record_result(st_, "x", ok=False, duration_s=0.1, taints=False, files=0)
    repeat = gate.check(st_, "file_system_scribe", args, "y")
    assert repeat.action == "deny"
    assert repeat.policy == "repeat_failure"
    assert (
        gate.check(st_, "file_system_scribe", {"op": "read", "path": "other.txt"}, "z").action
        == "allow"
    )
    st_.tool_time_s = settings.max_tool_time_s
    assert gate.check(st_, "host_telemetry_monitor", {}, "t").policy == "caps"
    st2 = TurnGateState(files_written=settings.max_files_written_per_turn)
    assert gate.check(st2, "isolated_python_sandbox", {"code": "1"}, "f").policy == "caps"
    st3 = TurnGateState(tool_calls=settings.max_tool_iterations * settings.max_concurrent_tools)
    assert gate.check(st3, "host_telemetry_monitor", {}, "q").policy == "caps"


def test_record_result_updates_taint_and_counters() -> None:
    st_ = TurnGateState()
    ToolGate.record_result(st_, "none", True, 1.5, True, 0)
    assert st_.tainted is True
    assert st_.tool_calls == 1
    assert st_.tool_time_s == 1.5
    assert call_fingerprint("a", {"x": 1}) == call_fingerprint("a", {"x": 1})
    assert call_fingerprint("a", {"x": 1}) != call_fingerprint("a", {"x": 2})
