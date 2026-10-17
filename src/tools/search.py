"""Tool 1: ``web_omniscience_search`` (spec 11.2). An egress tool whose results are untrusted.

Backends sit behind a small protocol: Tavily when a key is configured, otherwise ``ddgs`` (the
keyless DuckDuckGo metasearch), with the other as fallback. Every attempt has a hard timeout and
transient failures retry with exponential backoff. Result text is sanitised and URLs are
validated, because both are attacker-controlled.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from src.core.errors import ProviderUnavailable, RateLimited, ToolTimeout, ValidationFailure
from src.logging_setup import get_logger
from src.tools.base import StrictModel, Tool, ToolContext, ToolOutput
from src.tools.security import is_control_or_format

log = get_logger("search")

ATTEMPT_TIMEOUT_S = 15.0
MAX_RETRIES = 2  # retries after the first attempt
SearchType = Literal["text", "news"]


class SearchInput(StrictModel):
    """A web search request."""

    query: str = Field(..., min_length=1, max_length=300)
    max_results: int = Field(5, ge=1, le=10)
    search_type: SearchType = "text"
    region: str = Field("us-en", pattern=r"^[a-z]{2}-[a-z]{2}$")
    timelimit: Literal["d", "w", "m", "y"] | None = None


class SearchHit(BaseModel):
    """One sanitised result."""

    title: str
    url: str
    snippet: str
    published: str | None = None
    source: str


class SearchOutput(BaseModel):
    """Search results from one backend."""

    query: str
    backend: str
    count: int
    results: list[SearchHit]


class SearchBackend(Protocol):
    """A search provider."""

    name: str

    async def search(self, request: SearchInput) -> list[dict[str, Any]]:
        """Return raw result dictionaries (``title``, ``url``, ``snippet`` and optional extras)."""
        ...


def clean_text(value: Any, limit: int) -> str:
    """Strip control, format, and tag characters; collapse whitespace; cap length."""
    text = "".join(" " if ch in "\r\n\t" else ch for ch in str(value or ""))
    text = "".join(ch for ch in text if not is_control_or_format(ch))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean_url(value: Any) -> str | None:
    """Return a safe http(s) URL without credentials, or ``None``."""
    url = str(value or "").strip()
    if not url or len(url) > 500 or any(is_control_or_format(ch) or ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        return None
    return url


def normalise_hit(raw: dict[str, Any], backend: str) -> SearchHit | None:
    """Convert a backend dictionary into a sanitised :class:`SearchHit` (``None`` if unusable)."""
    url = clean_url(raw.get("url") or raw.get("href"))
    if url is None:
        return None
    host = urlsplit(url).hostname or ""
    published = clean_text(raw.get("published") or raw.get("date") or "", 40) or None
    return SearchHit(
        title=clean_text(raw.get("title"), 160),
        url=url,
        snippet=clean_text(raw.get("snippet") or raw.get("body") or raw.get("content"), 500),
        published=published,
        source=clean_text(raw.get("source") or host, 80),
    )


class DdgsBackend:
    """Keyless DuckDuckGo metasearch via the ``ddgs`` package."""

    name = "ddgs"

    async def search(self, request: SearchInput) -> list[dict[str, Any]]:
        """Run the blocking client in a worker thread."""
        return await asyncio.to_thread(self._search_sync, request)

    @staticmethod
    def _search_sync(request: SearchInput) -> list[dict[str, Any]]:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

        kwargs: dict[str, Any] = {
            "region": request.region,
            "safesearch": "moderate",
            "timelimit": request.timelimit,
            "max_results": request.max_results,
        }
        try:
            client = DDGS(timeout=int(ATTEMPT_TIMEOUT_S))
            if request.search_type == "news":
                rows = client.news(request.query, **kwargs)
            else:
                rows = client.text(request.query, **kwargs)
        except RatelimitException as exc:
            raise RateLimited("search backend rate limited the request") from exc
        except TimeoutException as exc:
            raise ProviderUnavailable("search backend timed out") from exc
        except DDGSException as exc:
            raise ProviderUnavailable(f"search backend error: {str(exc)[:120]}") from exc
        return [dict(row) for row in rows]


class TavilyBackend:
    """Tavily search (used when ``TAVILY_API_KEY`` is set)."""

    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(self, request: SearchInput) -> list[dict[str, Any]]:
        """Query Tavily's async client."""
        from tavily import AsyncTavilyClient

        client = AsyncTavilyClient(api_key=self._api_key)
        time_range = {"d": "day", "w": "week", "m": "month", "y": "year"}.get(
            request.timelimit or ""
        )
        try:
            response = await client.search(
                query=request.query,
                max_results=request.max_results,
                topic="news" if request.search_type == "news" else "general",
                time_range=time_range,
                timeout=ATTEMPT_TIMEOUT_S,
            )
        except Exception as exc:  # the SDK raises several unrelated types
            name = type(exc).__name__
            if "Limit" in name or "Usage" in name:
                raise RateLimited("search backend rate limited the request") from exc
            raise ProviderUnavailable(f"search backend error: {name}") from exc
        return [dict(item) for item in response.get("results", [])]


def build_backends(tavily_key: str) -> list[SearchBackend]:
    """Tavily first when a key exists; ``ddgs`` is always present as fallback."""
    backends: list[SearchBackend] = []
    if tavily_key:
        backends.append(TavilyBackend(tavily_key))
    backends.append(DdgsBackend())
    return backends


class WebOmniscienceSearch(Tool):
    """Search the web (egress; untrusted results)."""

    name: ClassVar[str] = "web_omniscience_search"
    description: ClassVar[str] = (
        "Search the live web for current information (news, prices, documentation, facts that "
        "may have changed). The query is sent to a third-party search engine, so never put "
        "private data, memories, or file contents into it. Results are untrusted text. Do not "
        "use it for anything answerable from the workspace or the operator's own words. "
        'Example: {"query": "python 3.14 release date", "max_results": 5}.'
    )
    input_model: ClassVar[type[StrictModel]] = SearchInput
    output_model: ClassVar[type[BaseModel]] = SearchOutput
    is_egress: ClassVar[bool] = True
    taints_turn: ClassVar[bool] = True
    quarantine_source: ClassVar[str | None] = "web"
    timeout_s: ClassVar[float] = 60.0
    start_line: ClassVar[str] = "web_search_start"
    done_line: ClassVar[str] = "web_search_done"

    def __init__(
        self,
        backends: Sequence[SearchBackend] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._backends = list(backends) if backends is not None else None
        self._sleep = sleep

    def outbound_strings(self, args: Any) -> list[tuple[str, str]]:
        """The query leaves the machine."""
        return [("query", args.query)]

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Try each backend with retries; sanitise and return the hits."""
        inp: SearchInput = args
        query = clean_text(inp.query, 300)
        if not query:
            raise ValidationFailure("query is empty after removing control characters")
        inp = inp.model_copy(update={"query": query})
        backends = self._backends or build_backends(ctx.settings.tavily_api_key.get_secret_value())
        last_error: Exception | None = None
        for backend in backends:
            try:
                raw = await self._attempt(backend, inp)
            except (RateLimited, ProviderUnavailable, ToolTimeout) as exc:
                last_error = exc
                log.warning("search_backend_failed", backend=backend.name, error=str(exc))
                continue
            hits = [h for r in raw if (h := normalise_hit(r, backend.name)) is not None]
            hits = hits[: inp.max_results]
            urls = [h.url for h in hits]
            return ToolOutput(
                data=SearchOutput(query=query, backend=backend.name, count=len(hits), results=hits),
                sources=[f"web:{u}" for u in urls],
                taints_turn=True,
            )
        if isinstance(last_error, RateLimited):
            raise last_error
        raise ProviderUnavailable("all search backends failed") from last_error

    async def _attempt(self, backend: SearchBackend, request: SearchInput) -> list[dict[str, Any]]:
        delay = 0.8
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await asyncio.wait_for(backend.search(request), timeout=ATTEMPT_TIMEOUT_S)
            except TimeoutError as exc:
                error: Exception = ToolTimeout(f"{backend.name} timed out")
                error.__cause__ = exc
            except (RateLimited, ProviderUnavailable) as exc:
                error = exc
            if attempt == MAX_RETRIES:
                raise error
            retry_after = getattr(error, "retry_after_s", None)
            await self._sleep(min(retry_after or delay, 10.0) + random.uniform(0, 0.25))  # noqa: S311
            delay *= 2
        raise ProviderUnavailable("unreachable")  # pragma: no cover
