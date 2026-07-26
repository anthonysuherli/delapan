"""Ablation-arm context builders — every arm reuses the real renderer.

    closed_book ──► None
    production  ──► embed → match_findings → band_findings → render_preamble
                     (select_preamble's own flow, inlined; no telemetry)
    oracle      ──► gold findings ─► render_preamble (retrieval bypassed)
    full_context ─► all live findings ─► render_preamble (big budget)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from delapan.core.agent import preamble
from delapan.core.agent.preamble import assess_coverage, band_findings, render_preamble
from delapan.core.agent.synopsis import load_synopsis
from delapan.core.config import TiersConfig, get_config
from delapan.store import Store
from evals.models import Question

# Engine never escapes the id attribute, so this relies on finding ids never
# containing a `"` — true for every id generator in this codebase (uuid4 / slugs).
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
    store: Store,
    kb_id: str,
    question: Question,
    depth: str = "normal",
    full_context_cap: int = 60_000,
    oracle_budget: int | None = None,
) -> ArmContext:
    if arm == "closed_book":
        return ArmContext(xml=None, coverage=None, band_counts=None)

    if arm == "production":
        # Mirrors select_preamble's own flow (delapan/core/agent/preamble.py:132-176)
        # minus its schedule_record call — evals must never write access_events/
        # backlog telemetry. Composing the primitives directly here (rather than
        # calling select_preamble) also gives real per-band counts instead of a
        # fabricated one derived from the rendered xml.
        cfg = get_config().tiers
        qvec = await preamble.embed_text(question.question)
        rows = await store.match_findings(
            kb_id, qvec, get_config().search.max_limit, cfg.band3_min
        )
        bands = band_findings(rows or [], cfg)
        coverage = assess_coverage(bands, cfg)
        syn_row = load_synopsis(store, kb_id)
        synopsis = (syn_row or {}).get("content") or []
        xml = render_preamble(synopsis, bands, depth=depth, cfg=cfg)
        band_counts = {1: len(bands[1]), 2: len(bands[2]), 3: len(bands[3])}
        return ArmContext(
            xml=xml, coverage=coverage, band_counts=band_counts, injected_ids=_ids_in(xml)
        )

    if arm == "oracle":
        rows = [store.get_finding(kb_id, fid) for fid in question.gold_finding_ids]
        xml = _render_rows(rows, budget=oracle_budget)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))

    if arm == "full_context":
        listed = store.list_findings(kb_id, limit=_LIST_ALL_LIMIT)["findings"]
        rows = [store.get_finding(kb_id, r["id"]) for r in listed]
        xml = _render_rows(rows, budget=full_context_cap)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))

    raise ValueError(f"unknown arm: {arm}")
