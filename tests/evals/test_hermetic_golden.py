"""The hermetic eval tier: retrieval metrics over the existing golden sets.

Extends tests/test_golden_sets.py from pass/fail assertions to measured
quantities — the documented false-`rich` case becomes a tracked number."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evals.hermetic import golden_retrieval_metrics

GOLDEN = Path(__file__).parent.parent / "golden"


@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*.yaml")), ids=lambda p: p.stem)
async def test_golden_metrics(path, store):
    spec = yaml.safe_load(path.read_text())
    vectors = json.loads(path.with_suffix(".vectors.json").read_text())
    out = await golden_retrieval_metrics(spec, vectors, store)

    assert len(out["per_query"]) == len(spec["queries"])
    for row in out["per_query"]:
        assert 0.0 <= row["mrr"] <= 1.0 and 0.0 <= row["ndcg_at_5"] <= 1.0
        # the golden expectation stays a hard gate, same as test_golden_sets
        assert row["verdict"] == row["expected_verdict"], row["query"]
    cal = out["calibration"]
    assert set(cal) == {"rich_n", "rich_gold_injected_rate", "gap_n", "gap_false_rate"}
