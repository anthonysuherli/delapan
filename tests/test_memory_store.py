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


@pytest.mark.asyncio
async def test_resolution_events_record_new_id_and_details(store):
    org, pid = store.resolve_project("evt", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_resolution_events(
        kb,
        [
            {
                "op": "NOOP",
                "candidate_title": "dup",
                "target_finding_id": "t1",
                "new_finding_id": None,
                "details": {"merged_urls": ["http://a"], "confidence_before": 0.4,
                            "confidence_after": 0.64},
                "reason": "same fact",
            }
        ],
    )
    rows = store.list_resolution_events(kb)
    assert rows[0]["op"] == "NOOP"
    assert rows[0]["details"]["merged_urls"] == ["http://a"]
    assert rows[0]["new_finding_id"] is None


@pytest.mark.asyncio
async def test_update_finding_in_place_keeps_id(store):
    org, pid = store.resolve_project("updA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    emb0 = [0.01] * 1536
    ids = await store.insert_findings(
        [
            {
                "org_id": org,
                "kb_id": kb,
                "title": "old",
                "content": {"k": "v0"},
                "category": "fact",
                "confidence": 0.4,
                "tags": [],
                "provenance": [{"url": "a"}],
                "embedding": emb0,
            }
        ]
    )
    fid = ids[0]
    emb1 = [0.5] * 1536
    await store.update_finding(
        kb,
        fid,
        content={"k": "v1"},
        confidence=0.8,
        provenance=[{"url": "a"}, {"url": "b"}],
        embedding=emb1,
        title="new",
    )
    got = store.get_finding(kb, fid)
    assert got["id"] == fid                 # id is stable
    assert got["title"] == "new"
    assert got["confidence"] == 0.8
    assert got["content"] == {"k": "v1"}    # dict content round-trips
    assert len(got["provenance"]) == 2
    assert store.count_findings(kb) == 1    # overwrite, not a new row
    hits = await store.match_findings(kb, emb1, match_count=1, min_similarity=0.0)
    assert hits[0]["id"] == fid             # vector re-indexed to emb1
