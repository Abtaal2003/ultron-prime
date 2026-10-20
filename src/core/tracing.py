"""OpenTelemetry tracing (spec 12.4): a trace per turn, with node, model and tool call spans.

Off by default (``OTEL_ENABLED=false``) and free when off: every helper here falls back to a no-op
span, and the OpenTelemetry packages are only imported when tracing is switched on. Install them
with ``uv sync --extra otel``.

Span names and attributes follow the OpenTelemetry GenAI semantic conventions where they exist
(``chat {model}``, ``execute_tool {name}``, ``gen_ai.*`` attributes); those conventions are still
marked as in development upstream, so names may need updating as they settle. Ultron specific
attributes use the ``ultron.`` prefix.

Message content is **not** recorded unless ``OTEL_CAPTURE_CONTENT=true``, and even then it is
passed through the same secret redaction as the logs and truncated.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol, TypeVar

from src.config import Settings
from src.logging_setup import get_logger, redact_text

log = get_logger(__name__)

CONTENT_LIMIT = 500
SERVICE_NAME = "ultron-prime"


class SpanLike(Protocol):
    """The subset of the OpenTelemetry span API this project uses."""

    def set_attribute(self, key: str, value: Any) -> Any:
        """Attach one attribute."""
        ...

    def add_event(self, name: str, attributes: Any = None) -> Any:
        """Record a point-in-time event on the span."""
        ...

    def record_exception(self, exception: BaseException) -> Any:
        """Record an exception on the span."""
        ...


class _NoopSpan:
    """Used when tracing is off; every call does nothing."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def add_event(self, name: str, attributes: Any = None) -> None:
        return None

    def record_exception(self, exception: BaseException) -> None:
        return None


NOOP_SPAN = _NoopSpan()


class _State:
    tracer: Any = None
    provider: Any = None
    capture_content: bool = False
    secrets: tuple[str, ...] = ()


_state = _State()


def enabled() -> bool:
    """True when spans are being recorded."""
    return _state.tracer is not None


def setup_tracing(settings: Settings, exporter: Any = None) -> bool:
    """Start tracing if ``OTEL_ENABLED`` is set. Returns whether tracing is now on.

    ``exporter`` overrides the configured OTLP exporter (tests pass an in-memory one). Without an
    override, spans go to ``OTEL_EXPORTER_ENDPOINT`` over OTLP/HTTP; if that is empty, tracing stays
    off with a warning rather than collecting spans nobody can see.
    """
    shutdown_tracing()
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        log.warning("otel_unavailable", hint="install the extra: uv sync --extra otel")
        return False
    if exporter is not None:
        processor: Any = SimpleSpanProcessor(exporter)
    elif settings.otel_exporter_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            log.warning("otel_exporter_unavailable", hint="install the extra: uv sync --extra otel")
            return False
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint))
    else:
        log.warning("otel_no_endpoint", hint="set OTEL_EXPORTER_ENDPOINT to receive spans")
        return False
    try:
        version = importlib.metadata.version(SERVICE_NAME)
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME, "service.version": version})
    )
    provider.add_span_processor(processor)
    # A private provider, not the global one: tests can rebuild it, and nothing else in the
    # process is affected. Context still propagates through the shared OpenTelemetry context.
    _state.provider = provider
    _state.tracer = provider.get_tracer("ultron_prime")
    _state.capture_content = settings.otel_capture_content
    _state.secrets = tuple(settings.secret_values())
    log.info("otel_enabled", endpoint=settings.otel_exporter_endpoint or "custom exporter")
    return True


def shutdown_tracing() -> None:
    """Flush and stop the current provider, if any."""
    provider = _state.provider
    _state.provider = None
    _state.tracer = None
    if provider is not None:
        provider.shutdown()


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[SpanLike]:
    """A span that is the current span inside the ``with`` block (a no-op when tracing is off)."""
    tracer = _state.tracer
    if tracer is None:
        yield NOOP_SPAN
        return
    clean = {k: v for k, v in (attributes or {}).items() if v is not None}
    with tracer.start_as_current_span(
        name, attributes=clean, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException as exc:
            _mark(current, exc)
            raise


CONTROL_FLOW = frozenset({"GraphInterrupt", "GraphBubbleUp", "ParentCommand", "GeneratorExit"})
"""Exceptions that steer LangGraph or close a generator; they are not failures."""


def _mark(current: Any, exc: BaseException) -> None:
    """Record ``exc`` on a span: control flow as an attribute, real failures as errors."""
    name = type(exc).__name__
    if name in CONTROL_FLOW or any(c.__name__ in CONTROL_FLOW for c in type(exc).__mro__):
        current.set_attribute("ultron.control_flow", name)
        return
    if name == "CancelledError":
        current.set_attribute("ultron.cancelled", True)
        return
    from opentelemetry.trace import Status, StatusCode

    current.record_exception(exc)
    current.set_attribute("error.type", name)
    current.set_status(Status(StatusCode.ERROR, str(exc)[:200]))


@contextmanager
def detached_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[SpanLike]:
    """A child of the current span that is *not* made current.

    For async generators: making a span current across ``yield`` points would leak it into the
    consumer's context, so streaming code uses this instead.
    """
    tracer = _state.tracer
    if tracer is None:
        yield NOOP_SPAN
        return
    clean = {k: v for k, v in (attributes or {}).items() if v is not None}
    current = tracer.start_span(name, attributes=clean)
    try:
        yield current
    except BaseException as exc:
        _mark(current, exc)
        raise
    finally:
        current.end()


def current_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    """Add an event to whatever span is current (no-op when tracing is off)."""
    if _state.tracer is None:
        return
    from opentelemetry import trace

    clean = {k: v for k, v in (attributes or {}).items() if v is not None}
    trace.get_current_span().add_event(name, clean)


def content(value: str) -> str | None:
    """Text safe to attach to a span, or ``None`` when content capture is off."""
    if not _state.capture_content:
        return None
    text = redact_text(value, _state.secrets)
    return text if len(text) <= CONTENT_LIMIT else text[: CONTENT_LIMIT - 1] + "…"


S = TypeVar("S", contravariant=True)


class NodeFn(Protocol[S]):
    """A LangGraph node: an async callable taking the graph state as ``state``."""

    def __call__(self, state: S) -> Awaitable[dict[str, Any]]:
        """Run the node."""
        ...


def traced_node(name: str, fn: NodeFn[S]) -> NodeFn[S]:
    """Wrap a graph node so each run is a child span of the turn."""

    async def node(state: S) -> dict[str, Any]:
        with span(f"ultron.node.{name}", {"ultron.node": name}) as current:
            result = await fn(state)
            route = result.get("route")
            if route:
                current.set_attribute("ultron.route", str(route))
            return result

    node.__name__ = getattr(fn, "__name__", name)
    node.__doc__ = getattr(fn, "__doc__", None)
    return node
