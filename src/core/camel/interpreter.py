"""The restricted-language interpreter (spec 4.11).

A plan is a small Python subset. It is parsed with :mod:`ast`, checked against an allow-list of
node types, and then *walked* by this interpreter. ``exec`` and ``eval`` are never used, so the
plan can do nothing the interpreter has no code for. Every value carries a capability; in strict
mode a value computed under a branch or loop inherits the capability of the condition, so data
cannot leak through which branch ran.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.core.camel.capabilities import (
    CONSTANT_CAP,
    EMPTY_CAP,
    Capability,
    Source,
    Value,
    combine,
    content_caps,
    tool_capability,
)
from src.core.camel.policies import CapabilityPolicy, PolicyCall, Verdict

MAX_DEPTH = 40
MAX_COLLECTION = 100_000
SHOW = "show"
QUARANTINED = "query_quarantined"


class CamelError(Exception):
    """Base class for plan errors."""


class CamelSyntaxError(CamelError):
    """The plan is not valid in the restricted language."""

    def __init__(self, message: str, lineno: int | None = None) -> None:
        super().__init__(f"line {lineno}: {message}" if lineno else message)
        self.lineno = lineno


class CamelRuntimeError(CamelError):
    """The plan failed while running (a bad index, a failed tool, a limit)."""


class LimitExceeded(CamelRuntimeError):
    """A statement, iteration, size or tool-call cap was reached."""


class ToolFailed(CamelRuntimeError):
    """A tool the plan called returned an error."""


class PolicyViolation(CamelError):
    """A capability policy stopped a call and the operator did not approve it."""

    def __init__(self, tool: str, reason: str, *, denied: bool, outbound: str = "") -> None:
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason
        self.denied = denied
        self.outbound = outbound


@dataclass(frozen=True, slots=True)
class Limits:
    """Caps that keep a plan small and finite."""

    max_statements: int = 60
    max_iterations: int = 200
    max_tool_calls: int = 12
    max_string: int = 200_000


# ----------------------------------------------------------------------------- validation ----

_STMT_OK = (ast.Expr, ast.Assign, ast.AugAssign, ast.If, ast.For, ast.Pass)
_EXPR_OK = (
    ast.Name, ast.Constant, ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.keyword, ast.Subscript, ast.Slice,
    ast.List, ast.Tuple, ast.Dict, ast.ListComp, ast.DictComp, ast.comprehension, ast.Attribute,
)  # fmt: skip
_CTX_OK = (ast.Load, ast.Store)
_OPS_OK = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Not,
    ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot,
)  # fmt: skip
_CONST_OK = (str, int, float, bool, type(None))

BUILTINS: dict[str, Callable[..., Any]] = {
    "len": len,
    "sorted": sorted,
    "min": min,
    "max": max,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "round": round,
    "sum": sum,
    "any": any,
    "all": all,
    "list": list,
    "range": range,
}
METHODS = frozenset(
    [
        "get", "keys", "values", "items", "lower", "upper", "strip", "split", "splitlines",
        "join", "startswith", "endswith", "replace", "count", "title",
    ]
)  # fmt: skip
RESERVED = frozenset({*BUILTINS, SHOW, QUARANTINED, "True", "False", "None"})

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}
_CMP: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


@dataclass(slots=True)
class ProgramInfo:
    """What a validated plan intends to do (used for routing)."""

    tools: list[str] = field(default_factory=list)
    statements: int = 0
    quarantined_reads: int = 0

    @property
    def empty(self) -> bool:
        """True when the plan calls nothing at all (plain conversation)."""
        return not self.tools and not self.quarantined_reads


def _fail(node: ast.AST, message: str) -> CamelSyntaxError:
    return CamelSyntaxError(message, getattr(node, "lineno", None))


def _depth(node: ast.AST, level: int = 0) -> int:
    return max([level, *(_depth(c, level + 1) for c in ast.iter_child_nodes(node))])


def check_program(
    source: str, tool_names: frozenset[str] | set[str], limits: Limits | None = None
) -> tuple[ast.Module, ProgramInfo]:
    """Parse ``source`` and reject anything outside the restricted language."""
    limits = limits or Limits()
    if len(source) > limits.max_string:
        raise CamelSyntaxError("the plan is too long")
    try:
        module = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise CamelSyntaxError(exc.msg, exc.lineno) from exc
    if _depth(module) > MAX_DEPTH:
        raise CamelSyntaxError("the plan is nested too deeply")
    info = ProgramInfo()
    method_calls = {id(n.func) for n in ast.walk(module) if isinstance(n, ast.Call)}
    for node in ast.walk(module):
        if isinstance(node, ast.stmt):
            info.statements += 1
        _check_node(node, method_calls, tool_names)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in tool_names:
                info.tools.append(name)
                if node.args:
                    raise _fail(node, f"call {name} with keyword arguments only")
            elif name == QUARANTINED:
                info.quarantined_reads += 1
                _check_quarantine_call(node)
            elif name not in BUILTINS and name != SHOW:
                raise _fail(node, f"unknown function '{name}'")
    if info.statements > limits.max_statements:
        raise CamelSyntaxError(f"the plan has more than {limits.max_statements} statements")
    if len(info.tools) > limits.max_tool_calls:
        raise CamelSyntaxError(f"the plan calls more than {limits.max_tool_calls} tools")
    return module, info


def _check_quarantine_call(node: ast.Call) -> None:
    if len(node.args) != 2 or node.keywords:
        raise _fail(node, f"{QUARANTINED}(value, 'Schema') takes exactly two positional arguments")
    schema = node.args[1]
    if not (isinstance(schema, ast.Constant) and isinstance(schema.value, str)):
        raise _fail(node, "the schema name must be a string literal")


def _check_node(
    node: ast.AST, method_calls: set[int], tool_names: frozenset[str] | set[str]
) -> None:
    if isinstance(node, (ast.Module, *_OPS_OK)):
        return
    if isinstance(node, ast.expr_context):
        if not isinstance(node, _CTX_OK):
            raise _fail(node, "deleting names is not allowed")
        return
    if isinstance(node, ast.stmt):
        _check_statement(node, tool_names)
        return
    if not isinstance(node, _EXPR_OK):
        raise _fail(node, f"{type(node).__name__} is not allowed")
    if isinstance(node, ast.Name) and node.id.startswith("_"):
        raise _fail(node, "names may not start with an underscore")
    if isinstance(node, ast.Constant) and not isinstance(node.value, _CONST_OK):
        raise _fail(node, "only strings, numbers, booleans and None are allowed as literals")
    if isinstance(node, ast.Attribute):
        if id(node) not in method_calls:
            raise _fail(node, "attributes may only be used to call an allowed method")
        if node.attr.startswith("_") or node.attr not in METHODS:
            raise _fail(node, f"method '{node.attr}' is not allowed")
    if isinstance(node, ast.comprehension) and (node.is_async or len(node.ifs) > 3):
        raise _fail(node, "comprehension is not allowed")
    if isinstance(node, ast.ListComp | ast.DictComp) and len(node.generators) != 1:
        raise _fail(node, "comprehensions may have one 'for' clause")
    if isinstance(node, ast.Call):
        if any(k.arg is None for k in node.keywords):
            raise _fail(node, "** arguments are not allowed")
        if not isinstance(node.func, ast.Name | ast.Attribute):
            raise _fail(node, "only named functions and methods can be called")
    if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
        raise _fail(node, "format specifiers are not allowed")


def _check_statement(node: ast.stmt, tool_names: frozenset[str] | set[str]) -> None:
    if not isinstance(node, _STMT_OK):
        raise _fail(node, f"{type(node).__name__} statements are not allowed")
    if isinstance(node, ast.For) and node.orelse:
        raise _fail(node, "for/else is not allowed")
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _check_target(target, tool_names)
    if isinstance(node, ast.AugAssign):
        _check_target(node.target, tool_names, single=True)
    if isinstance(node, ast.For):
        _check_target(node.target, tool_names)


def _check_target(
    target: ast.expr, tool_names: frozenset[str] | set[str], *, single: bool = False
) -> None:
    names = list(target.elts) if isinstance(target, ast.Tuple) and not single else [target]
    for item in names:
        if not isinstance(item, ast.Name):
            raise _fail(target, "assign to plain names only")
        if item.id.startswith("_") or item.id in RESERVED or item.id in tool_names:
            raise _fail(item, f"'{item.id}' cannot be assigned")


# ------------------------------------------------------------------------------ interfaces ----


@dataclass(slots=True)
class PortResult:
    """What a tool port returns."""

    ok: bool
    data: Any = None
    error: str = ""


@dataclass(slots=True)
class PortCall:
    """One tool call handed to the port."""

    call_id: str
    name: str
    args: dict[str, Any]
    approved: bool = False
    tainted: bool = False


class ToolPort(Protocol):
    """Runs a tool through the existing gate and registry."""

    async def call(self, call: PortCall) -> PortResult:
        """Execute ``call``; never raise for ordinary tool failures."""
        ...


class Parser(Protocol):
    """The quarantined parser (Q-LLM): untrusted text in, schema-bound data out."""

    async def parse(self, text: str, schema: str) -> Any:
        """Return JSON data validated against the named schema."""
        ...


@dataclass(slots=True)
class ConfirmRequest:
    """What the operator is asked to approve."""

    call_id: str
    tool: str
    verdict: Verdict
    sources: dict[str, list[str]]
    values: dict[str, str]


Confirm = Callable[[ConfirmRequest], Awaitable[bool]]


@dataclass(slots=True)
class Shown:
    """A value the plan chose to report."""

    label: str
    data: Any
    cap: Capability


@dataclass(slots=True)
class CallRecord:
    """One executed (or refused) tool call."""

    call_id: str
    tool: str
    status: str
    detail: str = ""


@dataclass(slots=True)
class RunResult:
    """The outcome of running a plan."""

    shown: list[Shown] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    aborted: str = ""
    violation: PolicyViolation | None = None
    iterations: int = 0
    variables: dict[str, Value] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the plan ran to the end."""
        return not self.aborted


# ------------------------------------------------------------------------------ interpreter --


def _preview(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Interpreter:
    """Walks a validated plan, tracking capabilities, and calls tools through the port."""

    def __init__(
        self,
        *,
        tools: frozenset[str] | set[str],
        port: ToolPort,
        policy: CapabilityPolicy,
        operator_text: str,
        parser: Parser | None = None,
        confirm: Confirm | None = None,
        limits: Limits | None = None,
        strict: bool = True,
        tainted: bool = False,
    ) -> None:
        self.tools = frozenset(tools)
        self.port = port
        self.policy = policy
        self.operator_text = operator_text
        self.parser = parser
        self.confirm = confirm
        self.limits = limits or Limits()
        self.strict = strict
        self.tainted = tainted
        self.ambient: Capability = EMPTY_CAP
        self.env: dict[str, Value] = {}
        self.result = RunResult()
        self._calls = 0

    # -- public ------------------------------------------------------------------------------
    async def run(self, module: ast.Module) -> RunResult:
        """Execute ``module``. Policy and runtime failures end the run and are reported."""
        try:
            await self._block(module.body)
        except PolicyViolation as exc:
            self.result.aborted = f"policy: {exc}"
            self.result.violation = exc
        except CamelError as exc:
            self.result.aborted = str(exc)
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            ZeroDivisionError,
            AttributeError,
        ) as exc:
            self.result.aborted = f"{type(exc).__name__}: {exc}"
        self.result.variables = dict(self.env)
        return self.result

    # -- statements --------------------------------------------------------------------------
    async def _block(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            await self._stmt(stmt)

    async def _stmt(self, node: ast.stmt) -> None:
        if isinstance(node, ast.Pass):
            return
        if isinstance(node, ast.Expr):
            await self._eval(node.value)
        elif isinstance(node, ast.Assign):
            value = await self._eval(node.value)
            for target in node.targets:
                self._assign(target, value)
        elif isinstance(node, ast.AugAssign):
            assert isinstance(node.target, ast.Name)
            current = self._lookup(node.target.id, node)
            right = await self._eval(node.value)
            self.env[node.target.id] = self._binop(node.op, current, right)
        elif isinstance(node, ast.If):
            test = await self._eval(node.test)
            await self._under(test.cap, node.body if test.data else node.orelse)
        elif isinstance(node, ast.For):
            await self._for(node)

    async def _under(self, cap: Capability, body: list[ast.stmt]) -> None:
        saved = self.ambient
        if self.strict:
            self.ambient = saved.union(cap)
        try:
            await self._block(body)
        finally:
            self.ambient = saved

    def _iterate(self, value: Value, node: ast.AST) -> list[Value]:
        data = value.data
        if isinstance(data, dict):
            items: list[Any] = list(data.keys())
        elif isinstance(data, list | tuple | str | range):
            items = list(data)
        else:
            raise CamelRuntimeError(f"cannot loop over {type(data).__name__}")
        self.result.iterations += len(items)
        if self.result.iterations > self.limits.max_iterations:
            raise LimitExceeded(f"more than {self.limits.max_iterations} loop iterations")
        return [Value(item, value.cap) for item in items]

    async def _for(self, node: ast.For) -> None:
        iterable = await self._eval(node.iter)
        saved = self.ambient
        if self.strict:
            self.ambient = saved.union(iterable.cap)
        try:
            for item in self._iterate(iterable, node):
                self._assign(node.target, item)
                await self._block(node.body)
        finally:
            self.ambient = saved

    def _assign(self, target: ast.expr, value: Value) -> None:
        value = value.tainted_by(self.ambient) if self.strict else value
        if isinstance(target, ast.Name):
            self.env[target.id] = value
            return
        assert isinstance(target, ast.Tuple)
        data = value.data
        if not isinstance(data, list | tuple) or len(data) != len(target.elts):
            raise CamelRuntimeError("cannot unpack that value into those names")
        for elt, item in zip(target.elts, data, strict=True):
            self._assign(elt, Value(item, value.cap))

    def _lookup(self, name: str, node: ast.AST) -> Value:
        if name not in self.env:
            raise CamelRuntimeError(f"'{name}' is used before it is assigned")
        return self.env[name]

    # -- expressions -------------------------------------------------------------------------
    def _mk(self, data: Any, *caps: Capability) -> Value:
        cap = caps[0].union(*caps[1:]) if caps else CONSTANT_CAP
        if self.strict:
            cap = cap.union(self.ambient)
        self._size_check(data)
        return Value(data, cap)

    def _size_check(self, data: Any) -> None:
        if isinstance(data, str) and len(data) > self.limits.max_string:
            raise LimitExceeded("a string grew past the size limit")
        if isinstance(data, list | tuple | dict) and len(data) > MAX_COLLECTION:
            raise LimitExceeded("a collection grew past the size limit")

    async def _eval(self, node: ast.expr) -> Value:
        if isinstance(node, ast.Constant):
            return Value(node.value, CONSTANT_CAP).tainted_by(self.ambient)
        if isinstance(node, ast.Name):
            if node.id in ("True", "False", "None"):
                return self._mk({"True": True, "False": False, "None": None}[node.id])
            return self._lookup(node.id, node)
        if isinstance(node, ast.JoinedStr):
            return await self._fstring(node)
        if isinstance(node, ast.BinOp):
            left, right = await self._eval(node.left), await self._eval(node.right)
            return self._binop(node.op, left, right)
        if isinstance(node, ast.UnaryOp):
            operand = await self._eval(node.operand)
            return self._mk(_UNARY[type(node.op)](operand.data), operand.cap)
        if isinstance(node, ast.BoolOp):
            return await self._boolop(node)
        if isinstance(node, ast.Compare):
            return await self._compare(node)
        if isinstance(node, ast.IfExp):
            test = await self._eval(node.test)
            saved = self.ambient
            if self.strict:
                self.ambient = saved.union(test.cap)
            try:
                chosen = await self._eval(node.body if test.data else node.orelse)
            finally:
                self.ambient = saved
            return chosen.tainted_by(test.cap) if self.strict else chosen
        if isinstance(node, ast.Call):
            return await self._call(node)
        if isinstance(node, ast.Subscript):
            return await self._subscript(node)
        if isinstance(node, ast.List | ast.Tuple):
            items = [await self._eval(e) for e in node.elts]
            data = [i.data for i in items]
            return self._mk(
                data if isinstance(node, ast.List) else tuple(data), *(i.cap for i in items)
            )
        if isinstance(node, ast.Dict):
            return await self._dict(node)
        if isinstance(node, ast.ListComp | ast.DictComp):
            return await self._comprehension(node)
        raise CamelRuntimeError(f"{type(node).__name__} cannot be evaluated")

    async def _fstring(self, node: ast.JoinedStr) -> Value:
        parts: list[str] = []
        caps: list[Capability] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            else:
                assert isinstance(piece, ast.FormattedValue)
                inner = await self._eval(piece.value)
                parts.append(inner.data if isinstance(inner.data, str) else str(inner.data))
                caps.append(inner.cap)
        return self._mk("".join(parts), *caps)

    def _binop(self, op: ast.operator, left: Value, right: Value) -> Value:
        func = _BINOPS.get(type(op))
        if func is None:
            raise CamelRuntimeError("that operator is not allowed")
        if isinstance(op, ast.Mult) and any(
            isinstance(v.data, str | list | tuple) for v in (left, right)
        ):
            count = right.data if isinstance(left.data, str | list | tuple) else left.data
            seq = left.data if isinstance(left.data, str | list | tuple) else right.data
            if not isinstance(count, int) or count * len(seq) > self.limits.max_string:
                raise LimitExceeded("that repetition is too large")
        return self._mk(func(left.data, right.data), left.cap, right.cap)

    async def _boolop(self, node: ast.BoolOp) -> Value:
        saved = self.ambient
        caps: list[Capability] = []
        last: Value | None = None
        try:
            for index, operand in enumerate(node.values):
                last = await self._eval(operand)
                caps.append(last.cap)
                if self.strict:
                    self.ambient = self.ambient.union(last.cap)
                short = (not last.data) if isinstance(node.op, ast.And) else bool(last.data)
                if short and index < len(node.values) - 1:
                    break
        finally:
            self.ambient = saved
        assert last is not None
        return self._mk(last.data, *caps)

    async def _compare(self, node: ast.Compare) -> Value:
        left = await self._eval(node.left)
        caps = [left.cap]
        result = True
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = await self._eval(comparator)
            caps.append(right.cap)
            result = result and bool(_CMP[type(op)](left.data, right.data))
            left = right
        return self._mk(result, *content_caps(*caps))

    async def _subscript(self, node: ast.Subscript) -> Value:
        target = await self._eval(node.value)
        if isinstance(node.slice, ast.Slice):
            parts = [
                await self._eval(p) if p is not None else Value(None, CONSTANT_CAP)
                for p in (node.slice.lower, node.slice.upper, node.slice.step)
            ]
            index: Any = slice(*(p.data for p in parts))
            caps = [p.cap for p in parts]
        else:
            idx = await self._eval(node.slice)
            index, caps = idx.data, [idx.cap]
        try:
            data = target.data[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise CamelRuntimeError(f"cannot read {_preview(index)} from that value") from exc
        return self._mk(data, target.cap, *content_caps(*caps))

    async def _dict(self, node: ast.Dict) -> Value:
        out: dict[Any, Any] = {}
        caps: list[Capability] = []
        for key, val in zip(node.keys, node.values, strict=True):
            if key is None:
                raise CamelRuntimeError("** in a dict is not allowed")
            k, v = await self._eval(key), await self._eval(val)
            out[k.data] = v.data
            caps += [k.cap, v.cap]
        return self._mk(out, *caps)

    async def _comprehension(self, node: ast.ListComp | ast.DictComp) -> Value:
        gen = node.generators[0]
        iterable = await self._eval(gen.iter)
        saved_env, saved_amb = dict(self.env), self.ambient
        if self.strict:
            self.ambient = saved_amb.union(iterable.cap)
        out_list: list[Any] = []
        out_dict: dict[Any, Any] = {}
        caps = [iterable.cap]
        try:
            for item in self._iterate(iterable, node):
                self._assign(gen.target, item)
                keep = True
                for cond in gen.ifs:
                    test = await self._eval(cond)
                    caps.append(test.cap)
                    if self.strict:
                        self.ambient = self.ambient.union(test.cap)
                    if not test.data:
                        keep = False
                        break
                if not keep:
                    continue
                if isinstance(node, ast.ListComp):
                    elem = await self._eval(node.elt)
                    out_list.append(elem.data)
                    caps.append(elem.cap)
                else:
                    k, v = await self._eval(node.key), await self._eval(node.value)
                    out_dict[k.data] = v.data
                    caps += [k.cap, v.cap]
        finally:
            self.env = saved_env
            self.ambient = saved_amb
        return self._mk(out_list if isinstance(node, ast.ListComp) else out_dict, *caps)

    # -- calls -------------------------------------------------------------------------------
    async def _call(self, node: ast.Call) -> Value:
        func = node.func
        if isinstance(func, ast.Attribute):
            return await self._method(func, node)
        assert isinstance(func, ast.Name)
        name = func.id
        if name in self.tools:
            return await self._tool(name, node)
        if name == SHOW:
            return await self._show(node)
        if name == QUARANTINED:
            return await self._quarantined(node)
        return await self._builtin(name, node)

    async def _builtin(self, name: str, node: ast.Call) -> Value:
        args = [await self._eval(a) for a in node.args]
        kwargs = {k.arg: await self._eval(k.value) for k in node.keywords if k.arg}
        if name == "range":
            for arg in args:
                if not isinstance(arg.data, int) or abs(arg.data) > self.limits.max_iterations:
                    raise LimitExceeded("that range is too large")
        try:
            data = BUILTINS[name](*(a.data for a in args), **{k: v.data for k, v in kwargs.items()})
        except (TypeError, ValueError) as exc:
            raise CamelRuntimeError(f"{name}() failed: {exc}") from exc
        if isinstance(data, range):
            data = list(data)
        return self._mk(
            data, *content_caps(*(a.cap for a in args), *(v.cap for v in kwargs.values()))
        )

    async def _method(self, func: ast.Attribute, node: ast.Call) -> Value:
        target = await self._eval(func.value)
        args = [await self._eval(a) for a in node.args]
        kwargs = {k.arg: await self._eval(k.value) for k in node.keywords if k.arg}
        method = getattr(target.data, func.attr, None)
        if not callable(method):
            raise CamelRuntimeError(f"that value has no method '{func.attr}'")
        try:
            data = method(*(a.data for a in args), **{k: v.data for k, v in kwargs.items()})
        except (TypeError, ValueError, KeyError) as exc:
            raise CamelRuntimeError(f"{func.attr}() failed: {exc}") from exc
        if not isinstance(data, str | int | float | bool | list | dict | type(None)):
            data = list(data)
        return self._mk(
            data,
            target.cap,
            *content_caps(*(a.cap for a in args), *(v.cap for v in kwargs.values())),
        )

    async def _show(self, node: ast.Call) -> Value:
        if not node.args or len(node.args) > 2 or node.keywords:
            raise CamelRuntimeError("show(value, 'label') takes a value and an optional label")
        value = await self._eval(node.args[0])
        label = ""
        if len(node.args) == 2:
            label_value = await self._eval(node.args[1])
            label = str(label_value.data)[:80]
        cap = value.cap.union(self.ambient) if self.strict else value.cap
        self.result.shown.append(Shown(label, value.data, cap))
        return Value(None, CONSTANT_CAP)

    async def _quarantined(self, node: ast.Call) -> Value:
        if self.parser is None:
            raise CamelRuntimeError("no quarantined parser is available")
        value = await self._eval(node.args[0])
        schema_node = node.args[1]
        assert isinstance(schema_node, ast.Constant)
        text = value.data if isinstance(value.data, str) else repr(value.data)
        parsed = await self.parser.parse(text, str(schema_node.value))
        seen = Capability(frozenset({Source("quarantined", str(schema_node.value))}))
        return self._mk(parsed, value.cap, seen)

    async def _tool(self, name: str, node: ast.Call) -> Value:
        self._calls += 1
        if self._calls > self.limits.max_tool_calls:
            raise LimitExceeded(f"more than {self.limits.max_tool_calls} tool calls")
        call_id = f"camel_{self._calls}"
        args: dict[str, Value] = {}
        for kw in node.keywords:
            assert kw.arg is not None
            args[kw.arg] = (await self._eval(kw.value)).tainted_by(self.ambient)
        verdict = self.policy.check(
            PolicyCall(
                tool=name,
                args=args,
                ambient=self.ambient,
                operator_text=self.operator_text,
                tainted=self.tainted or bool(self.result.calls),
            )
        )
        approved = False
        if verdict.action == "deny":
            self.result.calls.append(CallRecord(call_id, name, "denied", verdict.reason))
            raise PolicyViolation(name, verdict.reason, denied=True, outbound=verdict.outbound)
        if verdict.action == "confirm":
            approved = await self._ask(call_id, name, verdict, args)
            if not approved:
                self.result.calls.append(CallRecord(call_id, name, "rejected", verdict.reason))
                raise PolicyViolation(name, verdict.reason, denied=False, outbound=verdict.outbound)
        plain = {k: v.data for k, v in args.items()}
        outcome = await self.port.call(
            PortCall(call_id, name, plain, approved, self.tainted or bool(self.result.calls))
        )
        if not outcome.ok:
            self.result.calls.append(CallRecord(call_id, name, "error", outcome.error))
            raise ToolFailed(f"{name} failed: {outcome.error}")
        self.result.calls.append(CallRecord(call_id, name, "ok"))
        return self._mk(
            outcome.data,
            tool_capability(name, call_id),
            *content_caps(*(v.cap for v in args.values())),
        )

    async def _ask(self, call_id: str, name: str, verdict: Verdict, args: dict[str, Value]) -> bool:
        if self.confirm is None:
            return False
        request = ConfirmRequest(
            call_id=call_id,
            tool=name,
            verdict=verdict,
            sources={k: v.cap.describe() for k, v in args.items()},
            values={k: _preview(v.data) for k, v in args.items()},
        )
        return await self.confirm(request)


def plan_capability(values: list[Value]) -> Capability:
    """Combined capability of several values (re-exported for callers)."""
    return combine(*values)
