"""Ablation-arm context builders — every arm reuses the real renderer.

    closed_book ──► None
    production  ──► select_preamble (real retrieval; surface=None, no telemetry)
    oracle      ──► gold findings ─► render_preamble (retrieval bypassed)
    full_context ─► all live findings ─► render_preamble (big budget)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from delapan.core.agent.preamble import render_preamble, select_preamble
from delapan.core.config import TiersConfig, get_config
from evals.models import Question

_FINDING_ID = re.compile(r'<finding id="([^"]+)"')
# list_findings(limit=None) falls back to its own 20-row default, not
# "unbounded" — pass this (the SQLiteStore hard ceiling, LIST_MAX_LIMIT) to
# actually get every live finding for the full_context arm.
_LIST_ALL_LIMIT = 1000


@dataclass(frozen=True)
class ArmContext:
    xml: str | None
    coverage: str | None
    band_counts: dict[int, int] | None
    injected_ids: list[str] = field(default_factory=list)


def _ids_in(xml: str) -> list[str]:
    return _FINDING_ID.findall(xml)


def _render_rows(rows: list[dict], budget: int | None = None) -> str:
    """Render arbitrary finding rows through the real preamble renderer."""
    for r in rows:
        r.setdefault("similarity", 1.0)
    cfg = get_config().tiers
    if budget is not None:
        cfg = TiersConfig(**{**cfg.model_dump(), "preamble_char_budget": budget})
    return render_preamble([], {1: rows, 2: [], 3: []}, depth="shallow", cfg=cfg)


async def build_context(
    arm: str,
    *,
    store,
    kb_id: str,
    question: Question,
    depth: str = "normal",
    full_context_cap: int = 60_000,
) -> ArmContext:
    if arm == "closed_book":
        return ArmContext(xml=None, coverage=None, band_counts=None)

    if arm == "production":
        # surface=None: no access_events/backlog writes — evals never pollute telemetry.
        xml, coverage = await select_preamble(
            question.question, store=store, kb_id=kb_id, depth=depth, surface=None
        )
        ids = _ids_in(xml)
        # band_counts from the rendered xml would be lossy; recompute cheaply:
        # injected ids are what matters downstream, coverage carries the verdict.
        return ArmContext(xml=xml, coverage=coverage, band_counts={1: len(ids)}, injected_ids=ids)

    if arm == "oracle":
        rows = [store.get_finding(kb_id, fid) for fid in question.gold_finding_ids]
        xml = _render_rows(rows)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))

    if arm == "full_context":
        listed = store.list_findings(kb_id, limit=_LIST_ALL_LIMIT)["findings"]
        rows = [store.get_finding(kb_id, r["id"]) for r in listed]
        xml = _render_rows(rows, budget=full_context_cap)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))

    raise ValueError(f"unknown arm: {arm}")
