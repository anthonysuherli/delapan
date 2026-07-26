"""Pure IR metrics + coverage-verdict calibration.

    retrieved ids × gold ids ──► P@K / R@K / MRR / NDCG
    per-query verdicts        ──► rich/gap calibration rates
"""

from __future__ import annotations

import math


def precision_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    top = retrieved[:k]
    return sum(1 for r in top if r in gold) / k if k else 0.0


def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return sum(1 for r in retrieved[:k] if r in gold) / len(gold)


def mrr(retrieved: list[str], gold: set[str]) -> float:
    for i, r in enumerate(retrieved, start=1):
        if r in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, r in enumerate(retrieved[:k], 1) if r in gold)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def verdict_calibration(records: list[dict]) -> dict:
    """rich_gold_injected_rate: P(all gold ids injected | verdict=rich).
    gap_false_rate: P(gold existed | verdict=gap). The documented false-`rich`
    failure mode is 1 - rich_gold_injected_rate."""
    rich = [r for r in records if r["verdict"] == "rich"]
    gap = [r for r in records if r["verdict"] == "gap"]
    rich_ok = [r for r in rich if set(r["gold_ids"]) <= set(r["injected_ids"])]
    gap_bad = [r for r in gap if r["gold_ids"]]
    return {
        "rich_n": len(rich),
        "rich_gold_injected_rate": len(rich_ok) / len(rich) if rich else 0.0,
        "gap_n": len(gap),
        "gap_false_rate": len(gap_bad) / len(gap) if gap else 0.0,
    }
