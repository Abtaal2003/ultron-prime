"""Hallucination evals (spec 5.3): fabrications die, grounded claims live, rates carry intervals."""

from __future__ import annotations

import pytest
from evals.stats import wilson

from evals.hallucination import CASES, Case, run_case

N = 5  # spec 5.3: each case runs N times (default 5)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
async def test_fabrications_never_reach_the_operator_and_grounded_claims_do(case: Case) -> None:
    result = await run_case(case, N)
    assert result.rate.trials == N
    assert result.ok, f"{case.id}: {result.rate} first reply: {result.sample!r}"


def test_every_probe_in_the_spec_is_present() -> None:
    ids = {c.id for c in CASES}
    assert {
        "nonexistent_file",
        "no_tool_cpu_guess",
        "archive_contents_unread",
        "unsourced_urls",
        "exact_quote_recall",
    } <= ids
    assert sum(c.grounded for c in CASES) >= 3  # controls exist, so blocking everything cannot pass


def test_wilson_interval_behaves_at_the_edges() -> None:
    zero, full = wilson(0, 5), wilson(5, 5)
    assert zero.low == 0.0 and 0.4 < zero.high < 0.5
    assert full.high == 1.0 and 0.5 < full.low < 0.6
    mid = wilson(50, 100)
    assert mid.low < 0.5 < mid.high and mid.high - mid.low < 0.2
    assert wilson(0, 0).trials == 0
    with pytest.raises(ValueError):
        wilson(6, 5)
    assert "95% CI" in str(mid) and mid.to_dict()["trials"] == 100
