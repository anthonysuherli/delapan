"""Hand-computed IR metric cases; verdict calibration on synthetic records."""
from __future__ import annotations

import math

from evals.scoring.retrieval import (
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    verdict_calibration,
)


def test_precision_recall_at_k():
    retrieved, gold = ["a", "b", "c", "d"], {"a", "c", "x"}
    assert precision_at_k(retrieved, gold, 2) == 0.5          # a of {a,b}
    assert precision_at_k(retrieved, gold, 4) == 0.5          # a,c of 4
    assert recall_at_k(retrieved, gold, 4) == 2 / 3           # a,c of {a,c,x}
    assert precision_at_k([], gold, 5) == 0.0
    assert recall_at_k(retrieved, set(), 4) == 0.0            # no gold → 0, not div/0


def test_mrr():
    assert mrr(["x", "a", "y"], {"a"}) == 0.5                 # first hit at rank 2
    assert mrr(["x", "y"], {"a"}) == 0.0


def test_ndcg_binary():
    # retrieved: hit, miss, hit → DCG = 1/log2(2) + 1/log2(4) = 1.5
    # ideal (2 golds): 1/log2(2) + 1/log2(3)
    dcg = 1.0 + 1.0 / math.log2(4)
    idcg = 1.0 + 1.0 / math.log2(3)
    assert ndcg_at_k(["a", "b", "c"], {"a", "c"}, 3) == dcg / idcg
    assert ndcg_at_k(["b"], {"a"}, 1) == 0.0


def test_verdict_calibration():
    records = [
        {"verdict": "rich", "gold_ids": ["a"], "injected_ids": ["a", "b"]},   # good rich
        {"verdict": "rich", "gold_ids": ["z"], "injected_ids": ["a", "b"]},   # false rich
        {"verdict": "gap", "gold_ids": ["a"], "injected_ids": []},            # false gap
        {"verdict": "gap", "gold_ids": [], "injected_ids": []},               # honest gap
    ]
    cal = verdict_calibration(records)
    assert cal["rich_n"] == 2 and cal["rich_gold_injected_rate"] == 0.5
    assert cal["gap_n"] == 2 and cal["gap_false_rate"] == 0.5
