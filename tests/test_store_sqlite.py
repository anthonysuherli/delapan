from __future__ import annotations

import pytest


def test_resolve_project_and_kb_find_or_create(store):
    org_id, project_id = store.resolve_project("repoA", create=True)
    assert org_id == "local"
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    org_id2, project_id2 = store.resolve_project("repoA", create=False)
    kb_id2 = store.resolve_kb(org_id2, project_id2, "main", create=False)
    assert (org_id, project_id, kb_id) == (org_id2, project_id2, kb_id2)


@pytest.mark.asyncio
async def test_finding_roundtrip_preserves_content_dict(store):
    org_id, project_id = store.resolve_project("repoB", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    emb = [0.01] * 1536
    rows = [
        {
            "id": "f0000001",
            "org_id": org_id,
            "kb_id": kb_id,
            "title": "T",
            "content": {"summary": "s", "facts": ["a", "b"]},
            "category": "fact",
            "confidence": 0.9,
            "tags": ["x"],
            "provenance": [{"url": "http://e"}],
            "embedding": emb,
        }
    ]
    ids = await store.insert_findings(rows)
    assert ids == ["f0000001"]
    assert store.count_findings(kb_id) == 1
    got = store.get_finding(kb_id, "f0000001")
    assert got["content"] == {"summary": "s", "facts": ["a", "b"]}
    hits = await store.match_findings(kb_id, emb, match_count=5, min_similarity=0.0)
    assert hits and hits[0]["id"] == "f0000001" and "similarity" in hits[0]


def test_synopsis_upsert_then_load(store):
    org_id, project_id = store.resolve_project("repoC", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    store.upsert_synopsis(kb_id, content=[{"h": "head"}], finding_count=3, model="m")
    syn = store.load_synopsis(kb_id)
    assert syn["content"] == [{"h": "head"}] and syn["finding_count_at_build"] == 3
