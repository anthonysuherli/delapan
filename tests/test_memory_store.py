from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_resolution_events_roundtrip(store):
    org, pid = store.resolve_project("rezA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_resolution_events(
        kb,
        [
            {"op": "ADD", "candidate_title": "X", "target_finding_id": None, "reason": "new"},
            {"op": "NOOP", "candidate_title": "Y", "target_finding_id": "f1", "reason": "dup"},
        ],
    )
    evs = store.list_resolution_events(kb)
    assert len(evs) == 2
    assert {e["op"] for e in evs} == {"ADD", "NOOP"}
    assert all("created_at" in e for e in evs)
    assert evs[0]["candidate_title"] == "Y"  # newest-first ordering


@pytest.mark.asyncio
async def test_resolution_events_scoped_by_kb(store):
    org, pid = store.resolve_project("rezB", create=True)
    kb1 = store.resolve_kb(org, pid, "main", create=True)
    kb2 = store.resolve_kb(org, pid, "other", create=True)
    await store.insert_resolution_events(kb1, [{"op": "ADD", "candidate_title": "A", "reason": ""}])
    assert len(store.list_resolution_events(kb1)) == 1
    assert store.list_resolution_events(kb2) == []
