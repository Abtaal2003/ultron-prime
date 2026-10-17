"""L4: the deterministic tool-call policy gate (spec 4.4, 4.9, 12.2).

The model only *requests* tools. This gate decides, in code: is the tool known and enabled, do
the arguments validate, are the per-turn caps respected, does the egress policy allow it, and
does a remote session need an extra confirmation? It never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from src.config import Settings
from src.core.guards.egress_policy import (
    FlowDecision,
    FlowPolicy,
    FlowRequest,
    render_outbound,
)
from src.tools.base import Tool, ToolContext, canonical_json, sha256_hex
from src.tools.registry import ToolRegistry


@dataclass(slots=True)
class TurnGateState:
    """Mutable per-turn counters the gate consults."""

    tainted: bool = False
    operator_text: str = ""
    is_remote: bool = False
    iterations: int = 0
    tool_calls: int = 0
    tool_time_s: float = 0.0
    egress_holds: int = 0
    files_written: int = 0
    quarantined_reads: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    approved: set[str] = field(default_factory=set)


@dataclass(slots=True)
class GateDecision:
    """The gate's answer for one requested call."""

    action: str  # allow | hold | deny
    reason: str = ""
    policy: str = ""
    outbound_string: str = ""
    error_kind: str = "policy"
    tool: Tool | None = None
    validated: dict[str, Any] | None = None
    themed_key: str | None = None


def call_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """Stable identity of a call, used to short-circuit repeated identical failures."""
    return sha256_hex(tool_name + canonical_json(args))


class ToolGate:
    """Validates and authorises tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        settings: Settings,
        flow_policy: FlowPolicy,
        disabled_at_runtime: set[str] | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.flow = flow_policy
        self.disabled = disabled_at_runtime if disabled_at_runtime is not None else set()

    def check(
        self, state: TurnGateState, tool_name: str, raw_args: dict[str, Any], call_id: str
    ) -> GateDecision:
        """Decide whether ``tool_name(raw_args)`` may run now."""
        tool = self.registry.get(tool_name)
        if tool is None:
            return GateDecision(
                "deny", f"unknown tool '{tool_name}'", "registry", error_kind="validation"
            )
        if not self.settings.tool_enabled(tool_name) or tool_name in self.disabled:
            return GateDecision(
                "deny",
                f"tool '{tool_name}' is disabled in Settings",
                "toggle",
                themed_key="tool_disabled",
                tool=tool,
            )
        try:
            validated = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            msg = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors()[:4]
            )
            return GateDecision(
                "deny", f"invalid arguments: {msg}", "schema", error_kind="validation", tool=tool
            )
        dumped = validated.model_dump(mode="json")
        fingerprint = call_fingerprint(tool_name, dumped)
        state.fingerprints[call_id] = fingerprint

        if (
            state.tool_calls
            >= self.settings.max_tool_iterations * self.settings.max_concurrent_tools
        ):
            return GateDecision("deny", "tool-call cap for this turn reached", "caps", tool=tool)
        if state.tool_time_s >= self.settings.max_tool_time_s:
            return GateDecision(
                "deny", "tool time budget for this turn is spent", "caps", tool=tool
            )
        if state.failures.get(fingerprint, 0) >= 1:
            return GateDecision(
                "deny",
                "this exact call already failed in this turn; change the arguments",
                "repeat_failure",
                tool=tool,
            )
        if tool_name in {"artifact_forge", "isolated_python_sandbox"} and (
            state.files_written >= self.settings.max_files_written_per_turn
        ):
            return GateDecision("deny", "file-write cap for this turn reached", "caps", tool=tool)

        if call_id in state.approved:
            return GateDecision(
                "allow", "approved by the operator", "operator", tool=tool, validated=dumped
            )

        # Remote sessions confirm sensitive tools (spec 6.5).
        if state.is_remote and tool_name in self.settings.remote_confirm_tools:
            return GateDecision(
                "hold",
                "this session is remote; confirm before running this tool",
                "remote",
                outbound_string=render_outbound(tool.outbound_strings(validated)) or tool_name,
                tool=tool,
                validated=dumped,
            )

        flow = self._flow(state, tool, validated)
        if flow.action != "allow":
            return GateDecision(
                flow.action,
                flow.reason,
                flow.policy,
                outbound_string=flow.outbound_string,
                tool=tool,
                validated=dumped,
            )
        return GateDecision("allow", flow.reason, flow.policy, tool=tool, validated=dumped)

    def _flow(self, state: TurnGateState, tool: Tool, validated: Any) -> FlowDecision:
        probe = ToolContext(
            settings=self.settings,
            operator_text=state.operator_text,
            tainted=state.tainted,
            is_remote=state.is_remote,
        )
        return self.flow.evaluate(
            FlowRequest(
                tool_name=tool.name,
                is_egress=tool.is_egress_call(validated, probe),
                outbound=tool.outbound_strings(validated),
                operator_text=state.operator_text,
                tainted=state.tainted,
                holds_so_far=state.egress_holds,
                max_holds=self.settings.egress_max_holds_per_turn,
            )
        )

    @staticmethod
    def record_result(
        state: TurnGateState,
        call_id: str,
        ok: bool,
        duration_s: float,
        taints: bool,
        files: int,
    ) -> None:
        """Update the turn counters after the call identified by ``call_id`` finishes."""
        state.tool_calls += 1
        state.tool_time_s += duration_s
        if taints:
            state.tainted = True
        state.files_written += files
        if not ok:
            key = state.fingerprints.get(call_id)
            if key is not None:
                state.failures[key] = state.failures.get(key, 0) + 1
