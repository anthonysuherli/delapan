from __future__ import annotations

import pytest

import scripts.dedup_backfill as bf
from delapan.core.memory.models import ResolutionDecision, ResolutionOp


async def _seed(store, kb, title, url, emb_axis=0.01):
    return (
        await store.insert_findings(
            [
                {
                    "kb_id": kb, "title": title, "content": f"body of {title}",
                    "category": "fact", "confidence": 0.4, "tags": [],
                    "provenance": [{"url": url}], "embedding": [emb_axis] * 1536,
                }
            ]
        )
    )[0]


async def _fake_embed(texts):
    """Deterministic stand-in for the real embedding client.

    ``plan_ops`` re-embeds each row's text to query ``match_findings``; the
    fixtures below seed findings with a fixed dummy vector directly (bypassing
    the embedding client), so the fake must return that same vector for the
    match against the DB to line up. Avoids a real network call in a unit test.
    """
    return [[0.01] * 1536 for _ in texts]


@pytest.mark.asyncio
async def test_neighbors_exclude_self_and_later_rows(store, monkeypatch):
    org, pid = store.resolve_project("bfA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    first = await _seed(store, kb, "Tavily pricing", "http://a")
    await _seed(store, kb, "Tavily pricing", "http://b")   # second, same topic

    seen: list[list[str]] = []

    async def _spy(store_, kb_, cands, embs, cfg, *, neighbor_sets=None):
        seen.append([n["id"] for n in (neighbor_sets or [[]])[0]])
        return [ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]

    monkeypatch.setattr(bf, "resolve", _spy)
    monkeypatch.setattr(bf, "embed_batch", _fake_embed)
    await bf.plan_ops(store, kb, _cfg())

    # The oldest row has nothing before it; the second sees exactly the first —
    # never itself (which would match at sim ~1.0) and never a later row.
    assert seen[0] == []
    assert seen[1] == [first]


@pytest.mark.asyncio
async def test_apply_noop_retires_the_duplicate_and_keeps_urls_live(store, monkeypatch):
    org, pid = store.resolve_project("bfB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    keeper = await _seed(store, kb, "Tavily pricing", "http://a")
    dupe = await _seed(store, kb, "Tavily pricing", "http://b")

    ops = [bf.BackfillOp(op="NOOP", candidate_id=dupe, target_id=keeper, reason="dup")]
    await bf.apply_ops(store, kb, ops)

    assert store.count_findings(kb) == 1                       # shrank
    assert store.get_finding(kb, dupe)["superseded_by"] == keeper
    urls = {p["url"] for p in store.get_finding(kb, keeper)["provenance"]}
    assert urls == {"http://a", "http://b"}                    # no url lost from live


@pytest.mark.asyncio
async def test_apply_update_makes_candidate_survivor_with_merged_urls(store):
    org, pid = store.resolve_project("bfC", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    old = await _seed(store, kb, "Pricing v1", "http://a")
    newer = await _seed(store, kb, "Pricing v2", "http://b")

    await bf.apply_ops(store, kb, [bf.BackfillOp(op="UPDATE", candidate_id=newer,
                                                 target_id=old, reason="refines")])

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, old)["superseded_by"] == newer   # old retired
    urls = {p["url"] for p in store.get_finding(kb, newer)["provenance"]}
    assert urls == {"http://a", "http://b"}                       # union survives live


@pytest.mark.asyncio
async def test_apply_supersede_keeps_only_candidate_provenance(store):
    org, pid = store.resolve_project("bfD", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    old = await _seed(store, kb, "Tavily is free", "http://old")
    newer = await _seed(store, kb, "Tavily is paid", "http://new")

    await bf.apply_ops(store, kb, [bf.BackfillOp(op="SUPERSEDE", candidate_id=newer,
                                                 target_id=old, reason="contradicts")])

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, old)["superseded_by"] == newer
    urls = {p["url"] for p in store.get_finding(kb, newer)["provenance"]}
    assert urls == {"http://new"}         # contradicted sources do NOT merge in


@pytest.mark.asyncio
async def test_apply_add_is_a_no_op(store):
    org, pid = store.resolve_project("bfE", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    fid = await _seed(store, kb, "Unique", "http://a")
    snapshot = store.get_finding(kb, fid)

    await bf.apply_ops(store, kb, [bf.BackfillOp(op="ADD", candidate_id=fid, target_id=None)])

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, fid) == snapshot   # the row already exists; keep it


def _cfg():
    from delapan.core.config import get_config

    get_config.cache_clear()
    return get_config()
