"""Token-cost math: usage × price_table → $.  Pure, no IO."""

from __future__ import annotations

from delapan.core.config import ModelPrice


def compute_cost(
    model: str, in_tok: int, out_tok: int, price_table: dict[str, ModelPrice]
) -> float:
    """$ for one call. Unknown model → 0.0 (flagged at report time)."""
    p = price_table.get(model)
    if p is None:
        return 0.0
    return in_tok / 1_000_000 * p.input + out_tok / 1_000_000 * p.output
