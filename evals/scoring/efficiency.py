"""Context-efficiency accounting: one pinned tokenizer, quality-per-token.

    preamble xml ──► count_tokens (o200k_base) ──► tokens_injected
    arm accuracies ──► efficiency_per_1k ──► Δacc / 1k tokens
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken

# One tokenizer for every run, ever: the metric is only compared across
# arms/runs, so an approximation is fine — changing it breaks comparability.
TOKENIZER = "o200k_base"


@lru_cache(maxsize=1)
def _enc() -> tiktoken.Encoding:
    return tiktoken.get_encoding(TOKENIZER)


def count_tokens(text: str) -> int:
    return len(_enc().encode(text)) if text else 0


def efficiency_per_1k(prod_acc: float, closed_acc: float, mean_tokens: float) -> float:
    """Accuracy gained over closed-book per 1k injected tokens."""
    if mean_tokens <= 0:
        return 0.0
    return (prod_acc - closed_acc) / (mean_tokens / 1000.0)
