from __future__ import annotations

import pytest


async def _seed(store, title="T", content="body", conf=0.4):
    org, pid = store.resolve_project("prim", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ids = await store.insert_findings(
        [
            {
                "org_id": org, "kb_id": kb, "title": title, "content": content,
                "category": "fact", "confidence": conf, "tags": [],
                "provenance": [{"url": "http://a"}], "embedding": [0.01] * 1536,
            }
        ]
    )
    return kb, ids[0]


@pytest.mark.asyncio
async def test_update_finding_none_keeps_content_and_title(store):
    kb, fid = await _seed(store, title="Original", content="keep me")
    await store.update_finding(
        kb, fid, confidence=0.64, provenance=[{"url": "http://a"}, {"url": "http://b"}]
    )
    row = store.get_finding(kb, fid)
    assert row["content"] == "keep me"      # not NULLed
    assert row["title"] == "Original"       # not NULLed
    assert row["confidence"] == 0.64
    assert len(row["provenance"]) == 2


@pytest.mark.asyncio
async def test_update_finding_sets_given_fields(store):
    kb, fid = await _seed(store)
    await store.update_finding(kb, fid, content="new body", title="New")
    row = store.get_finding(kb, fid)
    assert row["content"] == "new body"
    assert row["title"] == "New"
    assert row["confidence"] == 0.4         # untouched


@pytest.mark.asyncio
async def test_invalidate_finding_retires_in_place(store):
    kb, fid = await _seed(store)
    await store.invalidate_finding(kb, fid, superseded_by="other123")
    row = store.get_finding(kb, fid)        # history stays readable
    assert row["invalidated_at"]
    assert row["superseded_by"] == "other123"
    assert store.count_findings(kb) == 0    # gone from live
    assert await store.match_findings(kb, [0.01] * 1536, 10, 0.0) == []


@pytest.mark.asyncio
async def test_supersede_finding_inserts_then_retires_atomically(store):
    kb, old = await _seed(store, title="Old fact")
    new_row = {
        "kb_id": kb, "title": "New fact", "content": "corrected",
        "category": "fact", "confidence": 0.64, "tags": [],
        "provenance": [{"url": "http://b"}], "embedding": [0.02] * 1536,
    }
    new_id = await store.supersede_finding(kb, old, new_row)

    assert new_id != old
    assert store.count_findings(kb) == 1                      # net zero growth
    hits = await store.match_findings(kb, [0.02] * 1536, 10, 0.0)
    assert [h["id"] for h in hits] == [new_id]                # only the new row is live
    retired = store.get_finding(kb, old)
    assert retired["invalidated_at"]
    assert retired["superseded_by"] == new_id                 # forward pointer
    assert store.get_finding(kb, new_id)["valid_from"]        # stamped


@pytest.mark.asyncio
async def test_supersede_rolls_back_when_target_missing(store):
    kb, _ = await _seed(store)
    before = store.count_findings(kb)
    with pytest.raises(ValueError, match="not live"):
        await store.supersede_finding(
            kb, "nonexistent", {"kb_id": kb, "title": "X", "content": "y",
                                "category": "fact", "confidence": 0.4, "tags": [],
                                "provenance": [], "embedding": [0.03] * 1536}
        )
    assert store.count_findings(kb) == before   # no orphaned insert survives
