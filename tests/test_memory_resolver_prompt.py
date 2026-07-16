from __future__ import annotations

import pytest

from delapan.core.exploration.models import Finding
from delapan.core.memory.resolver import _candidate_block


def test_candidate_block_includes_neighbor_bodies():
    f = Finding(
        exploration_id="e",
        project_id="p",
        category="fact",
        title="Tavily pricing",
        content={"plan": "free 1000/mo"},
    )
    neighbors = [{"id": "abc", "similarity": 0.91, "title": "Tavily", "content": "costs $0"}]
    block = _candidate_block(0, f, neighbors)
    assert "free 1000/mo" in block          # candidate body
    assert "id=abc" in block                # neighbor identity
    assert "costs $0" in block              # neighbor body — the C1 fix


def test_candidate_block_tolerates_empty_neighbor_content():
    f = Finding(exploration_id="e", project_id="p", category="fact", title="T", content={"a": 1})
    block = _candidate_block(0, f, [{"id": "x", "similarity": 0.5, "title": "N", "content": {}}])
    assert "id=x" in block
    assert "(none)" not in block


@pytest.mark.asyncio
async def test_resolve_uses_supplied_neighbors_without_searching(monkeypatch):
    from delapan.core.config import MemoryConfig
    from delapan.core.memory.resolver import resolve

    class _NoSearchStore:
        async def match_findings(self, *a, **k):
            raise AssertionError("must not search when neighbor_sets is supplied")

    f = Finding(exploration_id="e", project_id="p", category="fact", title="T",
                content={"k": "v"})
    out = await resolve(_NoSearchStore(), "kb1", [f], [[0.01] * 1536],
                        MemoryConfig(), neighbor_sets=[[]])
    assert out[0].op.value == "ADD"          # no neighbors → short-circuit, no LLM
