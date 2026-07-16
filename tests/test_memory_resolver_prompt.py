from __future__ import annotations

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
