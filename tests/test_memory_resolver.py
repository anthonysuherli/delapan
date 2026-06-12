from __future__ import annotations

import pytest

from delapan.core.config import MemoryConfig
from delapan.core.exploration.models import Finding
from delapan.core.memory import resolver as resolver_mod
from delapan.core.memory.models import ResolutionBatch, ResolutionDecision, ResolutionOp


def _finding(title, content):
    return Finding(
        exploration_id="e", project_id="p", category="fact", title=title, content=content
    )


class _FakeStore:
    """Returns a preset neighbor list per match_findings call, in candidate order."""

    def __init__(self, neighbors_in_order):
        self._n = neighbors_in_order
        self.calls = 0

    async def match_findings(self, kb_id, query_embedding, match_count, min_similarity, categories=None):
        out = self._n[self.calls]
        self.calls += 1
        return out


@pytest.mark.asyncio
async def test_no_neighbors_short_circuits_to_add(monkeypatch):
    async def _never(**kwargs):
        raise AssertionError("LLM must not be called when no candidate has neighbors")

    monkeypatch.setattr(resolver_mod, "structured_completion", _never)
    store = _FakeStore([[], []])
    cands = [_finding("A", {"k": "1"}), _finding("B", {"k": "2"})]
    embs = [[0.0] * 1536, [0.0] * 1536]
    out = await resolver_mod.resolve(store, "kb", cands, embs, MemoryConfig())
    assert [d.op for d in out] == [ResolutionOp.ADD, ResolutionOp.ADD]


@pytest.mark.asyncio
async def test_update_decision_applied(monkeypatch):
    async def _fake(**kwargs):
        return ResolutionBatch(
            decisions=[
                ResolutionDecision(
                    candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id="f1", reason="refines"
                )
            ]
        )

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.UPDATE
    assert out[0].target_finding_id == "f1"


@pytest.mark.asyncio
async def test_unknown_target_demoted_to_add(monkeypatch):
    async def _fake(**kwargs):
        return ResolutionBatch(
            decisions=[
                ResolutionDecision(
                    candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id="ghost", reason="x"
                )
            ]
        )

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.ADD


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_add(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(resolver_mod, "structured_completion", _boom)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.ADD


@pytest.mark.asyncio
async def test_none_target_demoted_to_add(monkeypatch):
    async def _fake(**kwargs):
        return ResolutionBatch(
            decisions=[
                ResolutionDecision(candidate_index=0, op=ResolutionOp.DELETE, target_finding_id=None, reason="x")
            ]
        )

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.ADD


@pytest.mark.asyncio
async def test_mixed_batch_preserves_no_neighbor_add(monkeypatch):
    # candidate 0 has NO neighbors (stays ADD without LLM say-so); candidate 1 has one.
    async def _fake(**kwargs):
        return ResolutionBatch(
            decisions=[
                ResolutionDecision(candidate_index=1, op=ResolutionOp.UPDATE, target_finding_id="f1", reason="r")
            ]
        )

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[], [{"id": "f1", "title": "B", "similarity": 0.9}]])
    cands = [_finding("A", {"k": "1"}), _finding("B", {"k": "2"})]
    out = await resolver_mod.resolve(store, "kb", cands, [[0.0] * 1536, [0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.ADD
    assert out[1].op == ResolutionOp.UPDATE and out[1].target_finding_id == "f1"


@pytest.mark.asyncio
async def test_chunking_makes_one_call_per_chunk(monkeypatch):
    calls = {"n": 0}

    async def _fake(**kwargs):
        calls["n"] += 1
        return ResolutionBatch(decisions=[])  # no decisions → candidates stay ADD

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}],
                        [{"id": "f2", "title": "B", "similarity": 0.9}]])
    cfg = MemoryConfig(max_candidates_per_pass=1)
    cands = [_finding("A", {"k": "1"}), _finding("B", {"k": "2"})]
    out = await resolver_mod.resolve(store, "kb", cands, [[0.1] * 1536, [0.2] * 1536], cfg)
    assert calls["n"] == 2  # two chunks of size 1 → two LLM calls
    assert [d.op for d in out] == [ResolutionOp.ADD, ResolutionOp.ADD]
