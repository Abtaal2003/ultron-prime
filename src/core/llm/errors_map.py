"""Shared helpers for turning SDK exceptions into the application's provider errors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.core.errors import (
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    RateLimited,
    RequestTooLarge,
)

_TOO_LARGE = re.compile(r"request too large|reduce your message size|context.{0,20}too long", re.I)


def retry_after_seconds(headers: Mapping[str, Any] | None) -> float | None:
    """Parse a ``retry-after`` header (seconds) when present and sane."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return min(value, 86_400.0) if value is not None and value >= 0 else None


_TRY_AGAIN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)
_PER_DAY = re.compile(r"per day|\(TPD\)|\(RPD\)|daily", re.I)


def wait_from_message(message: str) -> float | None:
    """Groq says "Please try again in 31m14.5s" in the body; read that when there's no header."""
    if _PER_DAY.search(message):
        found = _TRY_AGAIN.search(message)
        if found and any(found.groups()):
            h, m, sec = (float(g) if g else 0.0 for g in found.groups())
            return h * 3600 + m * 60 + sec
        return 86_400.0  # a daily cap with no reset time: treat it as not soon
    found = _TRY_AGAIN.search(message)
    if found and any(found.groups()):
        h, m, sec = (float(g) if g else 0.0 for g in found.groups())
        return h * 3600 + m * 60 + sec
    return None


def from_status(
    status: int | None, message: str, headers: Mapping[str, Any] | None = None
) -> ProviderError:
    """Classify an HTTP status into a retryable or terminal provider error."""
    short = message[:200]
    if status == 413 or _TOO_LARGE.search(message):
        # Groq sends this as 413 or as 429; either way the same request can never pass.
        return RequestTooLarge("request too large for the model", detail=message[:400])
    if status == 429:
        wait = wait_from_message(message) if _PER_DAY.search(message) else None
        return RateLimited(
            "provider rate limit",
            retry_after_s=wait or retry_after_seconds(headers) or wait_from_message(message),
            detail=short,
        )
    if status is None or status >= 500 or status in {408, 409, 425}:
        return ProviderUnavailable("provider unavailable", detail=short)
    return ProviderRejected(f"provider rejected the request ({status})", detail=short)
