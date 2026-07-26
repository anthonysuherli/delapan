"""Small-n paired statistics — no scipy/numpy, exact and seeded.

    paired bools ──► paired_table ──► mcnemar_exact ──► p
    per-q scores ──► bootstrap_ci ──► (lo, hi)
"""

from __future__ import annotations

import math
import random


def paired_table(a: list[bool], b: list[bool]) -> tuple[int, int]:
    """Discordant-pair counts for paired outcomes on the same questions."""
    if len(a) != len(b):
        raise ValueError("paired outcomes must have equal length")
    b_count = sum(1 for x, y in zip(a, b) if x and not y)
    c_count = sum(1 for x, y in zip(a, b) if y and not x)
    return b_count, c_count


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial McNemar p-value over discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_ci(
    values: list[float],
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap CI on the mean; seeded for reproducible reports."""
    if not values:
        raise ValueError("bootstrap_ci needs at least one value")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples)
    )
    lo_i = int((alpha / 2) * n_resamples)
    hi_i = min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))
    return means[lo_i], means[hi_i]
