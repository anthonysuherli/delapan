"""Arm builders against the hermetic SQLite store; embeddings monkeypatched."""
from __future__ import annotations

import pytest

from evals.arms import ArmContext, build_context
from evals.models import Question

VEC_A = [1.0] + [0.0] * 1535   # topic A axis
VEC_B = [0.0, 1.0] + [0.0] * 1534


async def _seed(store) -> tuple[str, str]:
    org, pid = store.resolve_project("evals-arms", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [
            {"id": "fa", "org_id": org, "kb_id": kb, "title": "Alpha fact",
             "content": "delapan embeds with gemini-embedding-001.", "category": "fact",
             "confidence": 0.5, "tags": [], "provenance": [], "embedding": VEC_A},
            {"id": "fb", "org_id": org, "kb_id": kb, "title": "Beta fact",
             "content": "the preamble budget is 7000 chars.", "category": "fact",
             "confidence": 0.5, "tags": [], "provenance": [], "embedding": VEC_B},
        ]
    )
    return org, kb


Q = Question(id="q1", question="how does delapan embed?",
             reference_answer="gemini-embedding-001", gold_finding_ids=["fa"])


async def test_closed_book(store):
    _, kb = await _seed(store)
    ctx = await build_context("closed_book", store=store, kb_id=kb, question=Q)
    assert ctx == ArmContext(xml=None, coverage=None, band_counts=None, injected_ids=[])


async def test_oracle_contains_only_gold(store):
    _, kb = await _seed(store)
    ctx = await build_context("oracle", store=store, kb_id=kb, question=Q)
    assert ctx.injected_ids == ["fa"]
    assert "Alpha fact" in ctx.xml and "Beta fact" not in ctx.xml
    assert ctx.coverage is None  # oracle bypasses retrieval — no verdict


async def test_production_uses_real_retrieval(store, monkeypatch):
    _, kb = await _seed(store)

    async def fake_embed(text: str) -> list[float]:
        return VEC_A

    monkeypatch.setattr("delapan.core.agent.preamble.embed_text", fake_embed)
    ctx = await build_context("production", store=store, kb_id=kb, question=Q)
    assert ctx.coverage in {"rich", "sparse", "gap"}
    assert "fa" in ctx.injected_ids and "fb" not in ctx.injected_ids
    # Real per-band counts, not fabricated: fa's embedding is identical to the
    # (monkeypatched) query embedding, so its similarity is 1.0 — band 1.
    assert set(ctx.band_counts) == {1, 2, 3}
    assert ctx.band_counts[1] == 1


async def test_full_context_includes_everything(store):
    _, kb = await _seed(store)
    ctx = await build_context("full_context", store=store, kb_id=kb, question=Q)
    assert set(ctx.injected_ids) == {"fa", "fb"}


async def test_full_context_exceeds_list_findings_default_limit(store):
    """Pins the full_context arm against regressing to list_findings(limit=None),
    which silently caps at 20 rows — full_context must return every live finding."""
    org, pid = store.resolve_project("evals-arms-many", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ids = [f"f{i}" for i in range(25)]
    await store.insert_findings(
        [
            {"id": fid, "org_id": org, "kb_id": kb, "title": f"Fact {i}",
             "content": f"filler content {i}", "category": "fact",
             "confidence": 0.5, "tags": [], "provenance": [], "embedding": VEC_A}
            for i, fid in enumerate(ids)
        ]
    )
    q = Question(id="qmany", question="anything", reference_answer="", gold_finding_ids=[ids[0]])
    ctx = await build_context("full_context", store=store, kb_id=kb, question=q)
    assert set(ctx.injected_ids) == set(ids)


async def test_unknown_arm_rejected(store):
    _, kb = await _seed(store)
    with pytest.raises(ValueError, match="unknown arm"):
        await build_context("vibes", store=store, kb_id=kb, question=Q)
