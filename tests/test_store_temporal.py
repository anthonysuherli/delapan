from __future__ import annotations

import pytest


async def _seed(store, title="T"):
    org, pid = store.resolve_project("tmp", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ids = await store.insert_findings(
        [
            {
                "org_id": org,
                "kb_id": kb,
                "title": title,
                "content": "body",
                "category": "fact",
                "confidence": 0.5,
                "tags": [],
                "provenance": [{"url": "http://a"}],
                "embedding": [0.01] * 1536,
            }
        ]
    )
    return kb, ids[0]


@pytest.mark.asyncio
async def test_insert_stamps_valid_from(store):
    kb, fid = await _seed(store)
    row = store.get_finding(kb, fid)
    assert row["valid_from"]                       # stamped by the write path
    assert row["invalidated_at"] is None           # live
    assert row["superseded_by"] is None


@pytest.mark.asyncio
async def test_invalidated_rows_hidden_from_match_and_count(store):
    kb, fid = await _seed(store)
    assert store.count_findings(kb) == 1
    hits = await store.match_findings(kb, [0.01] * 1536, match_count=10, min_similarity=0.0)
    assert [h["id"] for h in hits] == [fid]

    store._conn.execute(
        "UPDATE findings SET invalidated_at = ? WHERE id = ?;", ("2026-07-16T00:00:00Z", fid)
    )
    store._conn.commit()

    assert store.count_findings(kb) == 0
    hits = await store.match_findings(kb, [0.01] * 1536, match_count=10, min_similarity=0.0)
    assert hits == []
    assert store.list_findings(kb)["count"] == 0
    assert store.list_findings(kb, include_invalidated=True)["count"] == 1
    assert store.get_finding(kb, fid)["invalidated_at"] == "2026-07-16T00:00:00Z"
