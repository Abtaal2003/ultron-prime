"""Repeat-detection evals (spec 4.8): thresholds per embedder, measured recall and false positives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.core.guards.repeat import load_thresholds
from src.core.memory.embeddings import HashingEmbedder

from evals.repeat import (
    BENIGN,
    PARAPHRASES,
    Calibration,
    calibrate,
    choose,
    similarities,
    write,
)


async def test_hashing_embedder_meets_the_false_positive_target() -> None:
    result = await calibrate(HashingEmbedder())
    assert result.fp_auto <= 0.02
    assert result.recall_judge >= 0.9  # nearly every paraphrase reaches the relation label
    assert 0.0 < result.judge < result.auto <= 0.99
    assert result.paraphrases == len(PARAPHRASES) >= 40 and result.benign == len(BENIGN) >= 40


async def test_sets_include_other_languages_and_typos() -> None:
    joined = " ".join(a + " " + b for a, b in PARAPHRASES)
    assert any(word in joined for word in ("Cómo", "Qu'est-ce", "Wie funktioniert"))
    assert "pyhton" in joined


async def test_the_benign_set_is_adversarial_by_design() -> None:
    """Near-duplicate surface forms with different intent: a weak embedder cannot split them."""
    sims = await similarities(HashingEmbedder(), BENIGN)
    assert max(sims) >= 0.8


def test_choose_respects_the_false_positive_allowance() -> None:
    benign = [0.1 * i for i in range(10)]  # 0.0 .. 0.9
    para = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95] * 5
    auto, judge = choose(para, benign, fp_target=0.1)
    assert sum(b >= auto for b in benign) / len(benign) <= 0.1
    assert judge <= auto - 0.05
    strict_auto, _ = choose(para, benign, fp_target=0.0)
    assert strict_auto > max(benign)


def test_write_merges_per_embedder_and_the_detector_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"repeat": {"other-model": {"auto": 0.8, "judge": 0.5}}}))
    entry = Calibration("hashing-256", 0.93, 0.3, 0.0, 0.95, 0.0, 0.9, 40, 40)
    write(entry, path)
    data = json.loads(path.read_text())
    assert set(data["repeat"]) == {"other-model", "hashing-256"}
    got = load_thresholds(path, "hashing-256")
    assert (got.auto, got.judge) == (0.93, 0.3)
    assert load_thresholds(path, "unknown-model").auto == 0.90  # placeholders until calibrated


def test_the_shipped_calibration_covers_the_default_test_embedder() -> None:
    got = load_thresholds(Path("data/calibration.json"), "hashing-256")
    assert got.judge < got.auto and got.auto != 0.90


@pytest.mark.parametrize("pair", PARAPHRASES[:3])
async def test_identical_text_is_always_a_repeat(pair: tuple[str, str]) -> None:
    sims = await similarities(HashingEmbedder(), ((pair[0], pair[0]),))
    assert sims[0] > 0.99
