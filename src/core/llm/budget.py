"""Daily spending cap (spec 7.4, "unbounded consumption").

Every model call is already written to ``usage_log`` with its token counts. This module prices
those tokens and refuses new calls once today's estimate reaches ``DAILY_BUDGET_USD``.

Prices are configuration, not a live lookup: set ``MODEL_PRICES_USD_PER_MTOK`` to what your
providers charge. Anything without a price uses ``DEFAULT_USD_PER_MTOK_IN`` and ``..._OUT``.
Cached tokens are charged at the full input price, which overestimates on purpose: a cap that
errs high stops early rather than late. "Today" is the UTC day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.config import Settings
from src.core.db import Database
from src.core.errors import ErrorKind, UltronError


class BudgetExceeded(UltronError):
    """Today's estimated model spend has reached the configured cap."""

    kind: ErrorKind = "rate_limit"
    retryable = False


@dataclass(frozen=True, slots=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float


def price_for(settings: Settings, provider: str, model: str) -> Price:
    """The configured price: exact ``provider:model``, then ``provider:*``, then the default."""
    table = settings.model_prices_usd_per_mtok
    for key in (f"{provider}:{model}", f"{provider}:*"):
        if key in table:
            pair = table[key]
            return Price(float(pair[0]), float(pair[1]))
    return Price(settings.default_usd_per_mtok_in, settings.default_usd_per_mtok_out)


def cost_usd(price: Price, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """Estimated cost of one call."""
    return ((input_tokens + cached_tokens) * price.input + output_tokens * price.output) / 1e6


def today_utc(now: datetime | None = None) -> str:
    """The current UTC date as stored at the start of ``created_at`` values."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")


class Budget:
    """Reads today's usage and enforces the cap."""

    def __init__(self, settings: Settings, db: Database | None) -> None:
        self.settings = settings
        self.db = db

    @property
    def cap(self) -> float:
        """The daily cap in dollars; 0 means no cap."""
        return self.settings.daily_budget_usd

    async def spent_today(self, now: datetime | None = None) -> float:
        """Estimated spend since 00:00 UTC."""
        if self.db is None:
            return 0.0
        rows = await self.db.fetchall(
            "SELECT provider, model, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
            "SUM(cache_read_tokens + cache_write_tokens) AS c FROM usage_log "
            "WHERE created_at >= ? GROUP BY provider, model",
            (today_utc(now),),
        )
        return sum(
            cost_usd(
                price_for(self.settings, r["provider"], r["model"]),
                int(r["i"] or 0),
                int(r["o"] or 0),
                int(r["c"] or 0),
            )
            for r in rows
        )

    async def check(self, now: datetime | None = None) -> None:
        """Raise :class:`BudgetExceeded` once today's spend reaches the cap."""
        if self.cap <= 0:
            return
        spent = await self.spent_today(now)
        if spent >= self.cap:
            raise BudgetExceeded(
                "the daily model budget is spent",
                detail=f"estimated ${spent:.2f} of ${self.cap:.2f} today (UTC)",
            )
