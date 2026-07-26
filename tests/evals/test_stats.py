"""Hand-computed McNemar + bootstrap sanity properties (fixed seed)."""
from __future__ import annotations

import math

import pytest

from evals.stats import bootstrap_ci, mcnemar_exact, paired_table


def test_paired_table():
    a = [True, True, False, False, True]
    b = [True, False, True, False, False]
    assert paired_table(a, b) == (2, 1)  # a-only: idx 1,4 → 2; b-only: idx 2 → 1


def test_mcnemar_exact_hand_case():
    # b=8, c=2, n=10: p = 2 * P(X <= 2 | Bin(10, .5)) = 2 * (1+10+45)/1024
    expected = 2 * (1 + 10 + 45) / 1024
    assert math.isclose(mcnemar_exact(8, 2), expected)
    assert math.isclose(mcnemar_exact(2, 8), expected)  # symmetric


def test_mcnemar_degenerate():
    assert mcnemar_exact(0, 0) == 1.0        # no discordant pairs → no evidence
    assert mcnemar_exact(5, 5) == pytest.approx(1.0)  # perfectly balanced, clipped


def test_bootstrap_ci_constant():
    lo, hi = bootstrap_ci([0.7] * 50, seed=1)
    assert lo == hi == 0.7


def test_bootstrap_ci_brackets_mean_and_is_deterministic():
    vals = [0.0] * 40 + [1.0] * 60
    ci1 = bootstrap_ci(vals, seed=42)
    ci2 = bootstrap_ci(vals, seed=42)
    assert ci1 == ci2
    assert ci1[0] < 0.6 < ci1[1]
    assert 0.4 < ci1[0] and ci1[1] < 0.8  # sane width for n=100
