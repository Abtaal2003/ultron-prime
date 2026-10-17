"""Ultron's structural defences as AgentDojo pipeline elements.

``EgressGate`` applies the same taint-aware flow policy the agent uses (spec 4.9): once a turn has
read any tool output, a call that can send data out must carry only strings traceable to the
operator's own message. ``L1Redact`` runs the L1 prefilter over tool output and withholds output
that addresses the assistant. Both are deterministic, so they can be measured against a model
that obeys every injection it sees.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import ToolsExecutor
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionCall, FunctionsRuntime
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    ChatToolResultMessage,
    get_text_content_as_str,
    text_content_block_from_string,
)
from src.core.guards.egress_policy import FlowRequest, TaintAwareEgressPolicy
from src.core.guards.prefilter import run_prefilter

Query = tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict[str, Any]]

READ_PREFIXES = ("get_", "read_", "search_", "list_", "check_", "find_", "view_")
ALWAYS_EGRESS = frozenset({"get_webpage", "get_web_page", "fetch_url"})
WITHHELD = "[tool output withheld: it contained instructions addressed to the assistant]"


def is_egress(tool_name: str) -> bool:
    """Whether a tool can change state or send data out (everything but plain reads)."""
    if tool_name in ALWAYS_EGRESS:
        return True
    return not tool_name.startswith(READ_PREFIXES)


def flatten_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Collect ``(argument path, string)`` pairs from nested tool arguments."""
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in flatten_strings(v, f"{prefix}.{k}".strip("."))]
    if isinstance(value, list | tuple):
        return [p for i, v in enumerate(value) for p in flatten_strings(v, f"{prefix}[{i}]")]
    return []


def has_read_tool_output(messages: Sequence[ChatMessage]) -> bool:
    """Any tool result in the transcript means the turn has read external content."""
    return any(m["role"] == "tool" for m in messages)


def _blocked(call: FunctionCall, reason: str) -> ChatToolResultMessage:
    return ChatToolResultMessage(
        role="tool",
        content=[text_content_block_from_string("")],
        tool_call_id=call.id,
        tool_call=call,
        error=f"HELD by egress policy: {reason}",
    )


class EgressGate(BasePipelineElement):
    """Run tool calls through the taint-aware egress policy before executing them."""

    name = "ultron_egress_gate"

    def __init__(
        self,
        inner: ToolsExecutor | None = None,
        policy: TaintAwareEgressPolicy | None = None,
    ) -> None:
        self.inner = inner or ToolsExecutor()
        self.policy = policy or TaintAwareEgressPolicy("confirm_tainted")

    def judge(self, call: FunctionCall, operator_text: str, tainted: bool) -> str | None:
        """Return a reason when the call must not run, else ``None``."""
        decision = self.policy.evaluate(
            FlowRequest(
                tool_name=call.function,
                is_egress=is_egress(call.function),
                outbound=flatten_strings(dict(call.args)),
                operator_text=operator_text,
                tainted=tainted,
                max_holds=10_000,
            )
        )
        return None if decision.action == "allow" else decision.reason

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict[str, Any] = {},
    ) -> Query:
        """Execute the allowed calls of the last assistant message and report the held ones."""
        last = messages[-1] if messages else None
        if last is None or last["role"] != "assistant" or not last["tool_calls"]:
            return query, runtime, env, messages, extra_args
        tainted = has_read_tool_output(messages)
        allowed: list[FunctionCall] = []
        held: list[ChatToolResultMessage] = []
        for call in last["tool_calls"]:
            reason = self.judge(call, query, tainted)
            if reason is None:
                allowed.append(call)
            else:
                held.append(_blocked(call, reason))
                extra_args.setdefault("ultron_held", []).append(call.function)
        results: list[ChatMessage] = []
        if allowed:
            trimmed = ChatAssistantMessage(
                role="assistant", content=last["content"], tool_calls=allowed
            )
            _, runtime, env, out, extra_args = self.inner.query(
                query, runtime, env, [*messages[:-1], trimmed], extra_args
            )
            results = list(out[len(messages) :])
        return query, runtime, env, [*messages, *results, *held], extra_args


class L1Redact(BasePipelineElement):
    """Withhold tool output the L1 prefilter reads as instructions to the assistant."""

    name = "ultron_l1_redact"

    def __init__(self, min_severity: int = 2) -> None:
        self.min_severity = min_severity

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict[str, Any] = {},
    ) -> Query:
        """Replace flagged tool results (those after the last assistant message)."""
        out: list[ChatMessage] = list(messages)
        for i in range(len(out) - 1, -1, -1):
            msg = out[i]
            if msg["role"] == "assistant":
                break
            if msg["role"] != "tool":
                continue
            text = get_text_content_as_str(msg["content"])
            if run_prefilter(text, untrusted=True).severity >= self.min_severity:
                out[i] = ChatToolResultMessage(
                    role="tool",
                    content=[text_content_block_from_string(WITHHELD)],
                    tool_call_id=msg["tool_call_id"],
                    tool_call=msg["tool_call"],
                    error=msg["error"],
                )
                extra_args.setdefault("ultron_redacted", []).append(msg["tool_call"].function)
        return query, runtime, env, out, extra_args


class ScrubHeld(BasePipelineElement):
    """Remove held calls from the transcript once the run is over.

    AgentDojo decides some outcomes from the calls the model *requested*, not the calls that ran.
    A call the gate refused did not happen, so it must not count. This runs last, after the final
    model reply, so the transcript the model saw during the run is never altered.
    """

    name = "ultron_scrub_held"

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict[str, Any] = {},
    ) -> Query:
        """Drop every call that has a ``HELD`` result from its assistant message."""
        held = [
            m["tool_call"]
            for m in messages
            if m["role"] == "tool" and (m["error"] or "").startswith("HELD by egress policy")
        ]
        if not held:
            return query, runtime, env, messages, extra_args
        out: list[ChatMessage] = []
        for msg in messages:
            if msg["role"] == "assistant" and msg["tool_calls"]:
                kept = [c for c in msg["tool_calls"] if not any(c is h for h in held)]
                msg = ChatAssistantMessage(
                    role="assistant", content=msg["content"], tool_calls=kept or None
                )
            out.append(msg)
        return query, runtime, env, out, extra_args
