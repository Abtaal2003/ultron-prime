"""The shared tool contract (spec 11.1).

Every tool declares a strict Pydantic input schema and a typed output schema, runs inside a
``try / except / else / finally`` error boundary, and returns a :class:`ToolResult`. Tools never
raise into the orchestrator: expected failures become typed ``ToolErrorInfo`` values and the
final guarded ``except Exception`` converts anything unexpected into an ``internal`` error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from src.core import tracing
from src.core.db import Database, new_id
from src.core.errors import (
    NotFound,
    PermissionFailure,
    ToolErrorInfo,
    ToolTimeout,
    UltronError,
    ValidationFailure,
)
from src.core.events import AgentEvent, utcnow_iso
from src.logging_setup import get_logger, redact_text

if TYPE_CHECKING:
    from src.config import Settings
    from src.core.services import Services

log = get_logger("tools")

MAX_RESULT_PREVIEW = 600


class StrictModel(BaseModel):
    """Base for tool input and output schemas: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Provenance(BaseModel):
    """Where a result's content came from (CaMeL hook, spec 4.11)."""

    sources: list[str] = []
    handles: list[str] = []
    untrusted: bool = False


class FileRef(BaseModel):
    """A file created or changed by a tool call."""

    file_id: str
    version: int
    path: str
    logical_name: str
    sha256: str
    mime: str
    size: int
    external_content: bool = False
    derived_from: list[str] = []
    change_note: str | None = None


class ToolResult(BaseModel):
    """What the orchestrator receives from every tool call."""

    ok: bool
    tool: str
    receipt_id: str
    data: dict[str, Any] | None = None
    error: ToolErrorInfo | None = None
    duration_ms: int
    truncated: bool = False
    taints_turn: bool = False
    quarantine: bool = False
    provenance: Provenance
    files: list[FileRef] = []

    def preview(self, limit: int = MAX_RESULT_PREVIEW) -> str:
        """Return a short, redacted, single-string preview for the UI."""
        if not self.ok and self.error is not None:
            body = f"{self.error.kind}: {self.error.message}"
        else:
            body = json.dumps(self.data, ensure_ascii=False, default=str)
        body = redact_text(body)
        return body if len(body) <= limit else body[: limit - 1] + "…"


@dataclass(slots=True)
class ToolOutput:
    """The value a tool's ``run`` returns on success."""

    data: BaseModel
    files: list[FileRef] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)
    truncated: bool = False
    taints_turn: bool | None = None  # None means "use the class default"
    # True when the data must be summarised by the quarantined reader before the main model
    # sees it. None means "use the class default" (the tool's ``quarantine_source`` is enabled
    # in QUARANTINE_SOURCES and QUARANTINE_MODE is not off).
    quarantine: bool | None = None


Emitter = Callable[[AgentEvent], Awaitable[None]]


@dataclass(slots=True)
class ToolContext:
    """Everything a tool may touch during one call."""

    settings: Settings
    services: Services | None = None
    db: Database | None = None
    chat_id: str | None = None
    turn_id: str | None = None
    call_id: str = field(default_factory=lambda: new_id("call"))
    operator_text: str = ""
    is_remote: bool = False
    tainted: bool = False
    emit: Emitter | None = None
    # Files the operator attached in this turn, by attachment id (set by the orchestrator).
    attachment_ids: list[str] = field(default_factory=list)
    # Per-call scratch space for resources that ``cleanup`` must release (processes, temp dirs).
    scratch: dict[str, Any] = field(default_factory=dict)

    async def publish(self, event: AgentEvent) -> None:
        """Emit an event when an emitter is wired (it is optional in unit tests)."""
        if self.emit is not None:
            await self.emit(
                event.model_copy(update={"turn_id": self.turn_id, "chat_id": self.chat_id})
            )


def canonical_json(value: Any) -> str:
    """Stable JSON used for hashing arguments and results."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Truncate ``text`` to ``limit`` characters, reporting whether anything was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class Tool(ABC):
    """Base class every tool extends.

    Subclasses set the class attributes and implement :meth:`run`. Callers use :meth:`execute`,
    which is the error boundary and must never raise (except for task cancellation).
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[StrictModel]]
    output_model: ClassVar[type[BaseModel]]
    is_egress: ClassVar[bool] = False
    taints_turn: ClassVar[bool] = False
    # Source name matched against QUARANTINE_SOURCES (for example "web"); None means never.
    quarantine_source: ClassVar[str | None] = None
    timeout_s: ClassVar[float] = 30.0
    # Keys of the themed-line table (src/core/persona/lines.py).
    start_line: ClassVar[str] = ""
    done_line: ClassVar[str] = ""
    error_line: ClassVar[str] = ""

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Perform the validated action. Raise :class:`UltronError` for expected failures."""

    async def cleanup(self, ctx: ToolContext) -> None:
        """Release per-call resources. Always runs from ``finally``; must not raise."""
        return

    def outbound_strings(self, args: Any) -> list[tuple[str, str]]:
        """Return ``(field_name, outbound_value)`` pairs for egress provenance checks."""
        return []

    def is_egress_call(self, args: Any, ctx: ToolContext) -> bool:
        """Whether *this call* can send data out (defaults to the class-level flag)."""
        return self.is_egress

    def effective_timeout(self, args: Any) -> float:
        """Return the wall-clock budget for this call."""
        return self.timeout_s

    # -- the error boundary --------------------------------------------------------------------
    async def execute(self, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate, run, and wrap the call. Never raises except ``CancelledError``."""
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": self.name,
            "gen_ai.tool.call.id": ctx.call_id,
            "ultron.turn_id": ctx.turn_id,
        }
        with tracing.span(f"execute_tool {self.name}", attrs) as current:
            result = await self._execute(raw_args, ctx)
            current.set_attribute("ultron.ok", result.ok)
            current.set_attribute("ultron.taints_turn", result.taints_turn)
            current.set_attribute("ultron.quarantine", result.quarantine)
            if result.error is not None:
                current.set_attribute("error.type", result.error.kind)
            return result

    async def _execute(self, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.perf_counter()
        ok = False
        data: dict[str, Any] | None = None
        error: ToolErrorInfo | None = None
        files: list[FileRef] = []
        truncated = False
        taints = self.taints_turn
        quarantine = (
            self.quarantine_source is not None
            and ctx.settings.quarantine_mode != "off"
            and (
                ctx.settings.quarantine_mode == "strict"
                or self.quarantine_source in ctx.settings.quarantine_sources
            )
        )
        sources: list[str] = []
        handles: list[str] = []
        args_for_hash: Any = raw_args
        try:
            try:
                parsed = self.input_model.model_validate(raw_args)
            except ValidationError as exc:
                raise ValidationFailure("invalid arguments", detail=_short_validation(exc)) from exc
            args_for_hash = parsed.model_dump(mode="json")
            output = await asyncio.wait_for(
                self.run(parsed, ctx), timeout=self.effective_timeout(parsed)
            )
            if not isinstance(output.data, self.output_model):
                raise TypeError(
                    f"{self.name} returned {type(output.data).__name__}, "
                    f"expected {self.output_model.__name__}"
                )
        except asyncio.CancelledError:
            raise
        except UltronError as exc:
            error = exc.info()
        except TimeoutError:
            error = ToolTimeout(f"{self.name} exceeded {self.timeout_s:g}s", detail=None).info()
        except PermissionError as exc:
            error = PermissionFailure("permission denied by the operating system").info()
            log.warning("tool_permission_error", tool=self.name, error=str(exc))
        except FileNotFoundError:
            error = NotFound("file or directory not found").info()
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, LookupError) as exc:
            error = ToolErrorInfo(
                kind="internal", message=f"{type(exc).__name__}: {redact_text(str(exc))[:300]}"
            )
            log.warning("tool_failed", tool=self.name, error=str(exc))
        except Exception as exc:  # final guarded boundary: a tool must never raise
            error = ToolErrorInfo(kind="internal", message=f"unexpected {type(exc).__name__}")
            log.exception("tool_unexpected", tool=self.name)
        else:
            ok = True
            data = output.data.model_dump(mode="json")
            files = list(output.files)
            truncated = output.truncated
            sources = list(output.sources)
            handles = list(output.handles)
            if output.taints_turn is not None:
                taints = output.taints_turn
            if output.quarantine is not None:
                quarantine = output.quarantine
        finally:
            await self._safe_cleanup(ctx)
            duration_ms = int((time.perf_counter() - started) * 1000)
            log.info(
                "tool_call",
                tool=self.name,
                ok=ok,
                duration_ms=duration_ms,
                chat_id=ctx.chat_id,
                turn_id=ctx.turn_id,
                call_id=ctx.call_id,
            )
        receipt_id = await self._receipt(ctx, args_for_hash, data if ok else error, ok)
        return ToolResult(
            ok=ok,
            tool=self.name,
            receipt_id=receipt_id,
            data=data,
            error=error,
            duration_ms=duration_ms,
            truncated=truncated,
            taints_turn=taints,
            quarantine=quarantine and ok,
            provenance=Provenance(sources=sources, handles=handles, untrusted=taints),
            files=files,
        )

    async def _safe_cleanup(self, ctx: ToolContext) -> None:
        try:
            await self.cleanup(ctx)
        except Exception:  # cleanup must not mask the real outcome
            log.exception("tool_cleanup_failed", tool=self.name)

    async def _receipt(self, ctx: ToolContext, args: Any, result: Any, ok: bool) -> str:
        """Create and (when a database is wired) store a tool receipt (spec 5.2)."""
        receipt_id = new_id("rcpt")
        if ctx.db is None:
            return receipt_id
        digest = sha256_hex(
            canonical_json(result.model_dump() if isinstance(result, BaseModel) else result)
        )
        try:
            await ctx.db.execute(
                "INSERT INTO receipts(id, chat_id, turn_id, tool, args_hash, result_digest, ok, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    ctx.chat_id,
                    ctx.turn_id,
                    self.name,
                    sha256_hex(canonical_json(args)),
                    digest,
                    int(ok),
                    utcnow_iso(),
                ),
            )
        except Exception:  # a receipt failure must not turn a good result into a failure
            log.exception("receipt_store_failed", tool=self.name)
        return receipt_id

    # -- schema export -------------------------------------------------------------------------
    def json_schema(self) -> dict[str, Any]:
        """Return the model-facing input schema (no titles, refs inlined)."""
        from src.core.llm.structured import simplify_schema

        return simplify_schema(self.input_model.model_json_schema())


def _short_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
