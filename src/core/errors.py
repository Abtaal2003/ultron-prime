"""Typed error hierarchy and the shared ``ToolErrorInfo`` shape.

Errors are data at every boundary: tools never raise into the orchestrator, the API never leaks a
stack trace, and the UI receives a machine-readable ``code`` plus a human-readable ``message``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ErrorKind = Literal[
    "validation",
    "policy",
    "timeout",
    "not_found",
    "permission",
    "rate_limit",
    "unavailable",
    "limit",
    "conflict",
    "internal",
]


class ToolErrorInfo(BaseModel):
    """Serialisable description of a failed tool call."""

    kind: ErrorKind
    message: str
    retryable: bool = False
    detail: str | None = None


class UltronError(Exception):
    """Base class for every error this application raises on purpose."""

    kind: ErrorKind = "internal"
    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def info(self) -> ToolErrorInfo:
        """Convert the exception into the wire representation."""
        return ToolErrorInfo(
            kind=self.kind, message=self.message, retryable=self.retryable, detail=self.detail
        )


class ValidationFailure(UltronError):
    """Input failed schema or semantic validation."""

    kind: ErrorKind = "validation"


class PolicyDenied(UltronError):
    """A security policy refused the action."""

    kind: ErrorKind = "policy"


class ToolTimeout(UltronError):
    """A tool exceeded its wall-clock budget."""

    kind: ErrorKind = "timeout"
    retryable = True


class NotFound(UltronError):
    """The requested object does not exist (or is hidden by privacy rules)."""

    kind: ErrorKind = "not_found"


class PermissionFailure(UltronError):
    """The operating system or an ACL denied access."""

    kind: ErrorKind = "permission"


class LimitExceeded(UltronError):
    """A size, count, or budget limit was exceeded."""

    kind: ErrorKind = "limit"


class Conflict(UltronError):
    """The request conflicts with current state (for example a concurrent turn)."""

    kind: ErrorKind = "conflict"


class ProviderError(UltronError):
    """An LLM provider call failed."""

    kind: ErrorKind = "unavailable"
    retryable = True


class RateLimited(ProviderError):
    """A provider returned a rate-limit response."""

    kind: ErrorKind = "rate_limit"

    def __init__(
        self, message: str, *, retry_after_s: float | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after_s = retry_after_s


class ProviderUnavailable(ProviderError):
    """A provider is down, overloaded, or unreachable."""


class ProviderRejected(ProviderError):
    """A provider refused the request itself (bad key, bad request); retrying will not help."""

    kind: ErrorKind = "internal"
    retryable = False


class StructuredOutputError(UltronError):
    """A model returned data that does not validate against the requested schema."""

    kind: ErrorKind = "validation"


class ConfigurationError(UltronError):
    """The configuration is invalid; raised at startup with a readable message."""

    kind: ErrorKind = "validation"
