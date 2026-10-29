"""The real-embedding recall check runs end to end on the offline embedder."""

from __future__ import annotations

from evals import embeddings_check


async def test_report_shape_on_the_hashing_embedder() -> None:
    out = await embeddings_check.run("hashing", write_calibration=False)
    recall = out["recall"]
    assert isinstance(recall, dict)
    assert set(recall["by_cutoff"]) == set(embeddings_check.CUTOFFS)
    assert len(recall["related_scores"]) == len(embeddings_check.RELATED)
    assert 0.0 <= recall["related_top1"] <= 1.0
    assert out["repeat"]["embedder"] == "hashing-256"  # type: ignore[index]
