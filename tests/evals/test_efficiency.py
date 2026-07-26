"""Token counting is pinned to o200k_base; efficiency math is hand-checked."""
from __future__ import annotations

import pytest

from evals.scoring.efficiency import TOKENIZER, count_tokens, efficiency_per_1k


def test_tokenizer_pinned():
    assert TOKENIZER == "o200k_base"


def test_count_tokens_deterministic_and_monotonic():
    short, long = count_tokens("hello world"), count_tokens("hello world " * 50)
    assert short == count_tokens("hello world")  # deterministic
    assert 0 < short < long
    assert count_tokens("") == 0


def test_efficiency_per_1k():
    # +20 points of accuracy for a mean 2000 injected tokens → 0.10 per 1k
    assert efficiency_per_1k(0.8, 0.6, 2000.0) == pytest.approx(0.10)
    assert efficiency_per_1k(0.8, 0.6, 0.0) == 0.0  # closed-book arm: no tokens
