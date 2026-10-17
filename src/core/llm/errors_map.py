"""Shared helpers for turning SDK exceptions into the application's provider errors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.core.errors import ProviderError, ProviderRejected, ProviderUnavailable, RateLimited


def retry_after_seconds(headers: Mapping[str, Any] | None) -> float | None:
    """Parse a ``retry-after`` header (seconds) when present and sane."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return min(value, 120.0) if value is not None and value >= 0 else None


def from_status(
    status: int | None, message: str, headers: Mapping[str, Any] | None = None
) -> ProviderError:
    """Classify an HTTP status into a retryable or terminal provider error."""
    short = message[:200]
    if status == 429:
        return RateLimited(
            "provider rate limit", retry_after_s=retry_after_seconds(headers), detail=short
        )
    if status is None or status >= 500 or status in {408, 409, 425}:
        return ProviderUnavailable("provider unavailable", detail=short)
    return ProviderRejected(f"provider rejected the request ({status})", detail=short)
