from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from delapan.core.config import CurationConfig
from delapan.core.curation.backlog import rank_backlog

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _row(rid: str, *, recurrence: int, coverage: str, age_days: float) -> dict:
    return {
        "id": rid,
        "query_text": f"q-{rid}",
        "recurrence": recurrence,
        "coverage": coverage,
        "last_seen": (NOW - timedelta(days=age_days)).isoformat(),
    }


def test_empty_input():
    assert rank_backlog([], CurationConfig(), NOW) == []


def test_recurrence_orders():
    cfg = CurationConfig()
    rows = [
        _row("a", recurrence=1, coverage="gap", age_days=0),
        _row("b", recurrence=5, coverage="gap", age_days=0),
    ]
    assert [r["id"] for r in rank_backlog(rows, cfg, NOW)] == ["b", "a"]


def test_gap_outranks_sparse_at_equal_recurrence():
    cfg = CurationConfig()
    rows = [
        _row("sparse", recurrence=3, coverage="sparse", age_days=0),
        _row("gap", recurrence=3, coverage="gap", age_days=0),
    ]
    assert [r["id"] for r in rank_backlog(rows, cfg, NOW)] == ["gap", "sparse"]


def test_two_half_lives_scores_one_quarter():
    cfg = CurationConfig()  # recency_half_life_days = 14
    fresh = _row("fresh", recurrence=1, coverage="gap", age_days=0)
    old = _row("old", recurrence=1, coverage="gap", age_days=28)
    ranked = {r["id"]: r["score"] for r in rank_backlog([fresh, old], cfg, NOW)}
    assert ranked["old"] == pytest.approx(ranked["fresh"] * 0.25, rel=1e-6)


def test_unparseable_last_seen_does_not_raise():
    cfg = CurationConfig()
    rows = [{"id": "x", "recurrence": 2, "coverage": "gap", "last_seen": "not-a-date"}]
    out = rank_backlog(rows, cfg, NOW)
    assert len(out) == 1 and out[0]["score"] >= 0.0


def test_aware_last_seen_with_naive_now():
    """Timezone-aware last_seen with naive now should not raise TypeError."""
    cfg = CurationConfig()
    # Create a naive now (no tzinfo)
    now_naive = datetime(2026, 7, 16)  # noqa: DTZ001 — naive on purpose
    # Create row with timezone-aware last_seen (UTC offset in ISO string)
    rows = [{"id": "x", "recurrence": 2, "coverage": "gap", "last_seen": "2026-07-15T00:00:00+00:00"}]
    # Should not raise; should return a valid score
    out = rank_backlog(rows, cfg, now_naive)
    assert len(out) == 1 and out[0]["score"] >= 0.0


def test_naive_last_seen_string():
    """Fully naive last_seen (no UTC offset in ISO string) should work."""
    cfg = CurationConfig()
    # Create a naive now (no tzinfo)
    now_naive = datetime(2026, 7, 16)  # noqa: DTZ001 — naive on purpose
    # Create row with naive last_seen (no timezone offset)
    rows = [{"id": "x", "recurrence": 2, "coverage": "gap", "last_seen": "2026-07-14T00:00:00"}]
    # Should not raise; should return a valid score
    out = rank_backlog(rows, cfg, now_naive)
    assert len(out) == 1 and out[0]["score"] >= 0.0
