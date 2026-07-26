"""Backlog ranking — pure scoring over curation_topics rows.

    rows ─► score = recurrence × coverage_weight × 0.5 ^ (age_days/half_life) ─► sorted

No IO: the store fetches, this ranks. Kept pure so the ranking is unit-testable
without a database and so both the MCP tool and the HTTP route share one rule.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from delapan.core.config import CurationConfig


def _age_days(last_seen: str, now: datetime) -> float:
    """Age in days; unparseable/absent timestamps count as fresh (0.0)."""
    try:
        seen = datetime.fromisoformat(last_seen)

        # Normalize tzinfo to handle all combinations of naive/aware datetimes
        now_dt = now
        if seen.tzinfo is None and now.tzinfo is not None:
            # seen is naive, now is aware: assume seen is in now's timezone
            seen = seen.replace(tzinfo=now.tzinfo)
        elif seen.tzinfo is not None and now.tzinfo is None:
            # seen is aware, now is naive: assume now is UTC
            now_dt = now.replace(tzinfo=UTC)

        return max(0.0, (now_dt - seen).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return 0.0


def rank_backlog(rows: list[dict], cfg: CurationConfig, now: datetime) -> list[dict]:
    """Rank open topics by demand × severity × freshness, descending.

    Each returned row is the input dict plus a float ``score``. Rows are not
    mutated in place — callers get copies."""
    half_life = max(cfg.recency_half_life_days, 1)
    out: list[dict] = []
    for r in rows:
        weight = cfg.gap_weight if r.get("coverage") == "gap" else cfg.sparse_weight
        # True half-life decay: score is exactly halved every half_life days.
        # exp(-ln(2) * age/half_life) ≡ 0.5 ** (age/half_life)
        decay = math.exp(-math.log(2) * _age_days(r.get("last_seen") or "", now) / half_life)
        score = float(r.get("recurrence") or 0) * weight * decay
        out.append({**r, "score": score})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out
