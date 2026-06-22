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


@pytest.mark.asyncio
async def test_finding_roundtrip_preserves_content_string(store):
    """Some ingests store ``content`` as a plain markdown string, not a JSON
    dict. The read path must return it verbatim — not silently drop a
    non-JSON string to ``{}`` (which strands the finding's body)."""
    org_id, project_id = store.resolve_project("repoStr", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    emb = [0.02] * 1536
    body = "**Regulation**: Cap. 41R, Part 4\n\nValuation basis is prescribed."
    rows = [
        {
            "id": "f0000002",
            "org_id": org_id,
            "kb_id": kb_id,
            "title": "T2",
            "content": body,
            "category": "fact",
            "confidence": 0.8,
            "tags": [],
            "provenance": [],
            "embedding": emb,
        }
    ]
    await store.insert_findings(rows)
    got = store.get_finding(kb_id, "f0000002")
    assert got["content"] == body  # get_finding read path
    hits = await store.match_findings(kb_id, emb, match_count=5, min_similarity=0.0)
    assert hits and hits[0]["content"] == body  # match_findings read path


def test_synopsis_upsert_then_load(store):
    org_id, project_id = store.resolve_project("repoC", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    store.upsert_synopsis(kb_id, content=[{"h": "head"}], finding_count=3, model="m")
    syn = store.load_synopsis(kb_id)
    assert syn["content"] == [{"h": "head"}] and syn["finding_count_at_build"] == 3


@pytest.mark.asyncio
async def test_get_finding_global_ignores_kb_scope(store):
    org_id, project_id = store.resolve_project("repoG", create=True)
    kb_a = store.resolve_kb(org_id, project_id, "a", create=True)
    kb_b = store.resolve_kb(org_id, project_id, "b", create=True)
    await store.insert_findings(
        [
            {
                "id": "g1",
                "kb_id": kb_a,
                "title": "G",
                "content": {"s": "x"},
                "category": "fact",
                "confidence": 0.5,
                "tags": [],
                "provenance": [],
            }
        ]
    )
    # Scoped lookup from a different KB misses; global lookup resolves by PK.
    with pytest.raises(Exception):
        store.get_finding(kb_b, "g1")
    assert store.get_finding_global("g1")["title"] == "G"
    with pytest.raises(Exception):
        store.get_finding_global("missing")
