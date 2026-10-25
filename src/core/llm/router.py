"""Role-based routing with retries, failover, vision routing, and usage logging (spec 7.3, 7.4).

A role (``main``, ``guard``, ...) maps to ``provider:model`` in configuration. The router tries
that route first, then the configured fallback chain, filtered by what the call needs (tools,
structured output, vision). Transient errors retry with exponential backoff and jitter; when a
route is given up on, a ``ProviderFailover`` event says so.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from src.config import RouteSpec, Settings, parse_route
from src.core import tracing
from src.core.db import Database, new_id
from src.core.errors import (
    ConfigurationError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    RequestTooLarge,
    StructuredOutputError,
    ToolCallRejected,
)
from src.core.events import AgentEvent, ProviderFailover, utcnow_iso
from src.core.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMProvider,
    ProviderCapabilities,
    SamplingPolicy,
    StreamChunk,
    T,
    ThinkingPart,
    Usage,
    has_images,
    images_as_text,
    strip_thinking,
)
from src.core.llm.budget import Budget
from src.core.persona.lines import THEMED
from src.logging_setup import get_logger

log = get_logger("llm.router")

JUDGE_ROLES = frozenset(
    {
        "guard",
        "memory_judge",
        "summarizer",
        "quarantine_reader",
        "quarantine_reader_vision",
        "persona_eval_judge",
        "redteam_judge",
    }
)
STRUCTURED_ROLES = JUDGE_ROLES - {"summarizer"}
VISION_ROLES = frozenset({"quarantine_reader_vision", "vision_fallback"})
Emit = Callable[[AgentEvent], Awaitable[None]]


@dataclass(slots=True)
class CallContext:
    """Where a call belongs (for events and the usage log)."""

    chat_id: str | None = None
    turn_id: str | None = None
    emit: Emit | None = None
    notes: list[str] = field(default_factory=list)


def family_of(provider: str) -> str:
    """The thinking family a provider's blocks belong to."""
    return "claude" if provider == "anthropic" else provider


class LLMRouter:
    """Resolve roles to providers and run calls with resilience."""

    def __init__(
        self,
        settings: Settings,
        providers: dict[str, LLMProvider],
        db: Database | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.db = db
        self._sleep = sleep
        self.budget = Budget(settings, db)

    # ---------------------------------------------------------------------------- config --
    def route_for(self, role: str) -> RouteSpec:
        """Return the configured route of ``role``."""
        routes = self.settings.role_routes()
        if role not in routes:
            raise ConfigurationError(f"unknown model role '{role}'")
        return parse_route(routes[role])

    def capabilities_of(self, route: RouteSpec) -> ProviderCapabilities | None:
        """Capabilities of ``route`` (``None`` when its provider has no key)."""
        provider = self.providers.get(route.provider)
        return provider.capabilities(route.model) if provider else None

    def capability_matrix(self) -> dict[str, dict[str, Any]]:
        """Per-role routing report shown in Settings."""
        report: dict[str, dict[str, Any]] = {}
        for role, value in self.settings.role_routes().items():
            route = parse_route(value)
            caps = self.capabilities_of(route)
            report[role] = {
                "route": value,
                "available": caps is not None,
                "capabilities": caps.model_dump(mode="json") if caps else None,
            }
        return report

    def check_roles(self) -> list[str]:
        """Return configuration problems: roles assigned to models that lack a needed ability."""
        problems: list[str] = []
        for role, value in self.settings.role_routes().items():
            caps = self.capabilities_of(parse_route(value))
            if caps is None:
                continue
            if role in STRUCTURED_ROLES and caps.structured_output == "none":
                problems.append(f"{role}: {value} has no structured output")
            if role in VISION_ROLES and not caps.vision:
                problems.append(f"{role}: {value} cannot read images")
            if role == "main" and not caps.tool_calling:
                problems.append(f"main: {value} has no tool calling")
        return problems

    def assert_valid(self) -> None:
        """Raise :class:`ConfigurationError` when ``check_roles`` finds problems."""
        problems = self.check_roles()
        if problems:
            raise ConfigurationError("invalid model routing: " + "; ".join(problems))

    def default_sampling(self, role: str) -> SamplingPolicy:
        """Sampling used when the caller did not choose any."""
        s = self.settings
        if role in JUDGE_ROLES:
            return SamplingPolicy(effort=s.effort_judges, temperature=0.0)
        return SamplingPolicy(effort=s.effort_conversation, temperature=s.temperature_narration)

    # ------------------------------------------------------------------------- planning --
    def _candidates(
        self, role: str, request: ChatRequest, *, structured: bool
    ) -> list[tuple[RouteSpec, LLMProvider, ProviderCapabilities]]:
        routes: list[RouteSpec] = [self.route_for(role)]
        wants_vision = has_images(request.messages)
        if wants_vision and role == "main":
            primary = self.capabilities_of(routes[0])
            if primary is None or not primary.vision:
                routes.insert(0, self.route_for("vision_fallback"))
        routes.extend(parse_route(r) for r in self.settings.fallback_chain)
        seen: set[str] = set()
        out: list[tuple[RouteSpec, LLMProvider, ProviderCapabilities]] = []
        for route in routes:
            key = str(route)
            if key in seen:
                continue
            seen.add(key)
            provider = self.providers.get(route.provider)
            if provider is None:
                continue
            caps = provider.capabilities(route.model)
            if request.tools and not caps.tool_calling:
                continue
            if structured and caps.structured_output == "none":
                continue
            if role in VISION_ROLES and not caps.vision:
                continue
            out.append((route, provider, caps))
        return out

    def _prepare(
        self, request: ChatRequest, role: str, route: RouteSpec, caps: ProviderCapabilities
    ) -> tuple[ChatRequest, bool]:
        """Adapt ``request`` for a route; returns the request and whether thinking was stripped."""
        messages = request.messages
        family = family_of(route.provider)
        had_foreign = any(
            isinstance(p, ThinkingPart) and p.family != family for m in messages for p in m.parts
        )
        keep = family if family in caps.thinking_portable_to else None
        messages = strip_thinking(messages, keep)
        if not caps.vision and has_images(messages):
            messages = images_as_text(messages)
        sampling = request.sampling
        if sampling.effort is None and sampling.temperature is None:
            sampling = self.default_sampling(role)
        prepared = request.model_copy(
            update={
                "model": route.model,
                "messages": messages,
                "sampling": sampling,
                "role": role,
                "timeout_s": min(request.timeout_s, self.settings.llm_request_timeout_s)
                if request.timeout_s != 60.0
                else self.settings.llm_request_timeout_s,
            }
        )
        return prepared, had_foreign

    # --------------------------------------------------------------------------- running --
    async def _with_retries(self, call: Callable[[], Awaitable[Any]], label: str) -> Any:
        attempts = self.settings.llm_max_retries + 1
        delay = self.settings.llm_backoff_base_s
        for attempt in range(attempts):
            try:
                return await call()
            except RateLimited as exc:
                if self._not_soon(exc):
                    raise
                error: ProviderError = exc
                wait = exc.retry_after_s if exc.retry_after_s is not None else delay
            except ProviderUnavailable as exc:
                error = exc
                wait = delay
            except TimeoutError as exc:
                error = ProviderUnavailable("provider timed out")
                error.__cause__ = exc
                wait = delay
            if attempt == attempts - 1:
                raise error
            log.warning("llm_retry", route=label, attempt=attempt + 1, error=error.message)
            await self._sleep(min(wait, self.settings.llm_max_wait_s) + random.uniform(0, 0.25))  # noqa: S311
            delay *= 2
        raise ProviderUnavailable("unreachable")  # pragma: no cover

    def _not_soon(self, exc: RateLimited) -> bool:
        """The limit lifts later than we'd wait (live run 2026-10-25 waited out a daily cap)."""
        return exc.retry_after_s is not None and exc.retry_after_s > self.settings.llm_max_wait_s

    async def _announce(
        self, ctx: CallContext, prev: str, nxt: str, reason: str, stripped: bool
    ) -> None:
        ctx.notes.append(f"{prev} -> {nxt}: {reason}")
        if ctx.emit is not None:
            await ctx.emit(
                ProviderFailover(
                    turn_id=ctx.turn_id,
                    chat_id=ctx.chat_id,
                    from_route=prev,
                    to_route=nxt,
                    reason=reason,
                    thinking_stripped=stripped,
                    themed_line=THEMED["provider_failover"],
                )
            )

    async def _log_usage(
        self, ctx: CallContext, role: str, provider: str, model: str, usage: Usage, started: float
    ) -> None:
        if self.db is None:
            return
        try:
            await self.db.execute(
                "INSERT INTO usage_log(id, chat_id, turn_id, role, provider, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, latency_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("use"),
                    ctx.chat_id,
                    ctx.turn_id,
                    role,
                    provider,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    int((time.perf_counter() - started) * 1000),
                    utcnow_iso(),
                ),
            )
        except Exception:  # usage logging must never fail a call
            log.exception("usage_log_failed")

    def _no_route(self, role: str) -> ProviderUnavailable:
        have = sorted(self.providers)
        return ProviderUnavailable(
            f"no available model for role '{role}'",
            detail=f"configured providers: {', '.join(have) or 'none'}",
        )

    @staticmethod
    def _span_attrs(role: str, route: RouteSpec, ctx: CallContext) -> dict[str, Any]:
        return {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": route.provider,
            "gen_ai.request.model": route.model,
            "ultron.role": role,
            "ultron.turn_id": ctx.turn_id,
        }

    @staticmethod
    def _span_usage(current: tracing.SpanLike, usage: Usage, model: str | None) -> None:
        current.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
        current.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
        if model:
            current.set_attribute("gen_ai.response.model", model)

    @staticmethod
    def _span_error(current: tracing.SpanLike, exc: BaseException) -> None:
        current.set_attribute("error.type", type(exc).__name__)
        current.record_exception(exc)

    async def chat(
        self, role: str, request: ChatRequest, ctx: CallContext | None = None
    ) -> ChatResponse:
        """Complete ``request`` using ``role``'s route, falling back along the chain."""
        ctx = ctx or CallContext()
        await self.budget.check()
        candidates = self._candidates(role, request, structured=False)
        if not candidates:
            raise self._no_route(role)
        last: ProviderError | None = None
        previous = ""
        for route, provider, caps in candidates:
            prepared, foreign = self._prepare(request, role, route, caps)
            if previous:
                await self._announce(
                    ctx, previous, str(route), last.message if last else "", foreign
                )
            started = time.perf_counter()
            with tracing.span(f"chat {route.model}", self._span_attrs(role, route, ctx)) as sp:
                try:
                    response: ChatResponse = await self._with_retries(
                        partial(provider.chat, prepared), str(route)
                    )
                except ProviderError as exc:
                    self._span_error(sp, exc)
                    last, previous = exc, str(route)
                    continue
                self._span_usage(sp, response.usage, response.model)
            await self._log_usage(ctx, role, route.provider, route.model, response.usage, started)
            return response
        raise ProviderUnavailable(
            "all model routes failed", detail=last.message if last else None
        ) from last

    async def stream(
        self, role: str, request: ChatRequest, ctx: CallContext | None = None
    ) -> AsyncIterator[StreamChunk]:
        """Stream a reply. Failover happens only before the first chunk is delivered."""
        ctx = ctx or CallContext()
        await self.budget.check()
        candidates = self._candidates(role, request, structured=False)
        if not candidates:
            raise self._no_route(role)
        last: ProviderError | None = None
        previous = ""
        for route, provider, caps in candidates:
            prepared, foreign = self._prepare(request, role, route, caps)
            if previous:
                await self._announce(
                    ctx, previous, str(route), last.message if last else "", foreign
                )
            started = time.perf_counter()
            delivered = False
            attempts = self.settings.llm_max_retries + 1
            delay = self.settings.llm_backoff_base_s
            for attempt in range(attempts):
                try:
                    with tracing.detached_span(
                        f"chat {route.model}", self._span_attrs(role, route, ctx)
                    ) as sp:
                        async for chunk in provider.stream(prepared):
                            delivered = True
                            if chunk.kind == "done" and chunk.response is not None:
                                self._span_usage(sp, chunk.response.usage, chunk.response.model)
                                await self._log_usage(
                                    ctx,
                                    role,
                                    route.provider,
                                    route.model,
                                    chunk.response.usage,
                                    started,
                                )
                            yield chunk
                    return
                except ProviderError as exc:
                    last = exc
                    if delivered or isinstance(exc, ToolCallRejected | RequestTooLarge):
                        # The caller can fix both: the model repairs its call, or the request
                        # is shrunk to the cap the error names. Failing over instead spent the
                        # small fallback quota on requests the primary would take (2026-10-25).
                        raise
                    if not exc.retryable or attempt == attempts - 1:
                        break
                    if isinstance(exc, RateLimited) and self._not_soon(exc):
                        break  # a daily cap: fail over now instead of waiting minutes
                    wait = (
                        exc.retry_after_s
                        if isinstance(exc, RateLimited) and exc.retry_after_s
                        else delay
                    )
                    jitter = random.uniform(0, 0.25)  # noqa: S311
                    log.warning(  # the streaming path used to retry without a trace
                        "llm_retry", route=str(route), attempt=attempt + 1, error=exc.message
                    )
                    await self._sleep(min(wait, self.settings.llm_max_wait_s) + jitter)
                    delay *= 2
            previous = str(route)
        if isinstance(last, RequestTooLarge):
            raise last  # the caller can shrink the request; "unavailable" would hide that
        raise ProviderUnavailable(
            "all model routes failed", detail=last.message if last else None
        ) from last

    async def structured(
        self, role: str, request: ChatRequest, schema: type[T], ctx: CallContext | None = None
    ) -> T:
        """Return a validated ``schema`` instance. Invalid output from one route tries the next."""
        ctx = ctx or CallContext()
        await self.budget.check()
        candidates = self._candidates(role, request, structured=True)
        if not candidates:
            raise self._no_route(role)
        last: Exception | None = None
        previous = ""
        for route, provider, caps in candidates:
            prepared, foreign = self._prepare(request, role, route, caps)
            if previous:
                await self._announce(ctx, previous, str(route), str(last), foreign)
            started = time.perf_counter()
            with tracing.span(f"chat {route.model}", self._span_attrs(role, route, ctx)) as sp:
                try:
                    value, usage = await self._with_retries(
                        partial(provider.structured, prepared, schema), str(route)
                    )
                except (ProviderError, StructuredOutputError) as exc:
                    self._span_error(sp, exc)
                    log.warning(
                        "structured_call_failed",
                        route=str(route),
                        schema=schema.__name__,
                        error=exc.message,
                        detail=(exc.detail or "")[:300],
                    )
                    last, previous = exc, str(route)
                    continue
                self._span_usage(sp, usage, route.model)
            await self._log_usage(ctx, role, route.provider, route.model, usage, started)
            return value  # type: ignore[no-any-return]
        if isinstance(last, StructuredOutputError):
            raise last
        raise ProviderUnavailable("all model routes failed", detail=str(last)) from last
