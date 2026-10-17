"""Capability policies: per-tool rules over where an argument came from (spec 4.11).

A policy never looks at what the model *said*; it looks at the capability tags the interpreter
attached to each argument. Three outcomes: ``allow``, ``confirm`` (the operator sees the exact
values and decides) and ``deny`` (no prompt could make it safe). The egress policy of spec 4.9
stays in the tool gate as a second, independent layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from src.core.guards.egress_policy import render_outbound

if TYPE_CHECKING:
    from src.core.camel.capabilities import Capability, Value

Action = Literal["allow", "confirm", "deny"]
WRITE_OPS = frozenset({"write", "append"})


@dataclass(slots=True)
class PolicyCall:
    """One planned tool call with the capability of each argument."""

    tool: str
    args: dict[str, Value]
    ambient: Capability
    operator_text: str = ""
    tainted: bool = False


@dataclass(slots=True)
class Verdict:
    """A policy's answer."""

    action: Action
    reason: str = ""
    policy: str = ""
    outbound: str = ""


ALLOW = Verdict("allow")
Rule = Callable[[PolicyCall], Verdict | None]


def _outbound(call: PolicyCall) -> str:
    return render_outbound([(k, str(v.data)) for k, v in call.args.items()])


def _confirm(call: PolicyCall, policy: str, reason: str) -> Verdict:
    return Verdict("confirm", reason, policy, _outbound(call))


def _untrusted(call: PolicyCall, names: Iterable[str]) -> list[str]:
    return [n for n in names if n in call.args and not call.args[n].cap.trusted]


def _search(call: PolicyCall) -> Verdict | None:
    query = call.args.get("query")
    if query is not None and query.cap.private:
        return _confirm(
            call,
            "no_private_data_in_search",
            "the search query carries private data (" + ", ".join(query.cap.describe()) + ")",
        )
    return None


def _ping(call: PolicyCall) -> Verdict | None:
    if _untrusted(call, ["host"]):
        return _confirm(call, "host_from_operator", "the host did not come from the operator")
    return None


def _scribe(call: PolicyCall) -> Verdict | None:
    op = call.args.get("op")
    if op is not None and not op.cap.trusted:
        return _confirm(call, "operation_from_operator", "untrusted data chose the operation")
    if op is not None and op.data in WRITE_OPS and _untrusted(call, ["path"]):
        return _confirm(
            call, "path_from_operator", "the output path did not come from the operator"
        )
    return None


def _forge(call: PolicyCall) -> Verdict | None:
    bad = _untrusted(call, ["action", "filename"])
    if bad:
        return _confirm(
            call, "path_from_operator", f"{' and '.join(bad)} did not come from the operator"
        )
    return None


def _propose_memory(call: PolicyCall) -> Verdict | None:
    text = call.args.get("candidate_text")
    if text is not None and not text.cap.has_operator_evidence:
        return Verdict(
            "deny",
            "the candidate was derived only from untrusted content",
            "memory_from_operator_evidence",
            _outbound(call),
        )
    return None


def _sandbox(call: PolicyCall) -> Verdict | None:
    if _untrusted(call, ["code"]):
        return _confirm(
            call, "code_from_operator", "the code to run did not come from the operator"
        )
    return None


DEFAULT_RULES: dict[str, Rule] = {
    "web_omniscience_search": _search,
    "network_ping_diagnostic": _ping,
    "file_system_scribe": _scribe,
    "artifact_forge": _forge,
    "propose_memory": _propose_memory,
    "isolated_python_sandbox": _sandbox,
}


class CapabilityPolicy:
    """Applies the rule for the called tool, or the generic rule for other state-changing tools."""

    def __init__(
        self,
        *,
        stateful_tools: Iterable[str] = (),
        rules: dict[str, Rule] | None = None,
    ) -> None:
        self.rules = dict(DEFAULT_RULES if rules is None else rules)
        self.stateful = frozenset(stateful_tools)

    def check(self, call: PolicyCall) -> Verdict:
        """Return the verdict for ``call``."""
        rule = self.rules.get(call.tool)
        if rule is not None:
            return rule(call) or ALLOW
        if call.tool in self.stateful:
            bad = _untrusted(call, call.args)
            if bad:
                return _confirm(
                    call,
                    "all_arguments_from_operator",
                    "these arguments did not come from the operator: " + ", ".join(bad),
                )
        return ALLOW
