"""Small statistics helpers for eval reporting (no numpy needed)."""

from __future__ import annotations

import math
from dataclasses import dataclass

Z_95 = 1.959963984540054


@dataclass(frozen=True, slots=True)
class Rate:
    """A proportion with its 95 percent Wilson score interval."""

    successes: int
    trials: int
    low: float
    high: float

    @property
    def value(self) -> float:
        """The observed proportion (0.0 when there were no trials)."""
        return self.successes / self.trials if self.trials else 0.0

    def __str__(self) -> str:
        return (
            f"{self.successes}/{self.trials} ({self.value:.1%}, "
            f"95% CI {self.low:.1%} to {self.high:.1%})"
        )

    def to_dict(self) -> dict[str, float | int]:
        """JSON-friendly form."""
        return {
            "successes": self.successes,
            "trials": self.trials,
            "rate": round(self.value, 4),
            "ci_low": round(self.low, 4),
            "ci_high": round(self.high, 4),
        }


def wilson(successes: int, trials: int, z: float = Z_95) -> Rate:
    """Wilson score interval; behaves sensibly at 0 and at ``trials`` (unlike the normal one)."""
    if trials <= 0:
        return Rate(0, 0, 0.0, 0.0)
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return Rate(successes, trials, max(0.0, centre - half), min(1.0, centre + half))
