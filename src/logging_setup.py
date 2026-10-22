"""Structured logging with secret redaction (spec 7.4, 12.4).

JSON output in production, a readable console renderer in development. Every event passes
through a redaction processor that replaces configured secrets and anything that looks like a
provider API key, so a key can never reach a log line even by accident.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[redacted]"

# Shapes of common provider credentials. Kept deliberately conservative to avoid false positives.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{40,}"),  # newer Google AI Studio keys
    re.compile(r"tvly-[A-Za-z0-9\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
)


def redact_text(text: str, secrets_to_hide: Iterable[str] = ()) -> str:
    """Replace known secrets and credential-shaped substrings in ``text``."""
    for secret in secrets_to_hide:
        if secret and len(secret) >= 6:
            text = text.replace(secret, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any, secrets_to_hide: Iterable[str] = ()) -> Any:
    """Recursively redact strings inside containers."""
    hidden = tuple(secrets_to_hide)
    if isinstance(value, str):
        return redact_text(value, hidden)
    if isinstance(value, Mapping):
        return {k: redact_value(v, hidden) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact_value(v, hidden) for v in value]
    return value


def _make_redactor(secrets_to_hide: Iterable[str]) -> Any:
    hidden = tuple(secrets_to_hide)

    def processor(
        _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for key, value in list(event_dict.items()):
            event_dict[key] = redact_value(value, hidden)
        return event_dict

    return processor


class _StderrLoggerFactory:
    """Write to whatever ``sys.stderr`` is at the time, not the stream seen at configure time."""

    def __call__(self, *args: Any) -> Any:
        return structlog.PrintLogger(sys.stderr)


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """A stdlib handler that also resolves ``sys.stderr`` lazily."""

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def configure_logging(
    level: str = "INFO",
    fmt: str = "pretty",
    secrets_to_hide: Iterable[str] = (),
    *,
    stdlib: bool = True,
) -> None:
    """Configure structlog and the stdlib root logger."""
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _make_redactor(secrets_to_hide),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=_StderrLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    if not stdlib:
        return
    logging.basicConfig(level=getattr(logging, level), handlers=[_StderrHandler()], force=True)
    for noisy in ("httpx", "httpcore", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def ensure_safe_logging(settings: Any) -> None:
    """Configure structlog if nothing has yet: secrets hidden, no locals in tracebacks.

    The CLI configures logging at startup, but code that builds a runtime some other way (a
    script, a test harness, an ASGI server pointed at the app factory) would otherwise get
    structlog's development defaults, whose tracebacks print local variables such as request
    headers. A live harness on 2026-10-22 leaked an API key into its own log exactly that way.
    The standard library's handlers are left alone.
    """
    if structlog.is_configured():
        return
    configure_logging(
        settings.log_level, settings.log_format, settings.secret_values(), stdlib=False
    )


def configure_from_settings(settings: Any) -> None:
    """Apply the configured level and format, and hide every configured secret.

    Called once at process start. Without it structlog falls back to its development defaults,
    whose tracebacks print local variables, which can include API keys.
    """
    configure_logging(settings.log_level, settings.log_format, settings.secret_values())


def get_logger(name: str) -> Any:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
