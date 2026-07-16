from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.config import get_config
from delapan.core.exploration.models import Finding
from delapan.core.memory import persist as persist_mod
from delapan.core.memory.models import ResolutionDecision, ResolutionOp


def _finding(pid, title, content):
    return Finding(exploration_id="e", project_id=pid, category="fact", title=title, content=content)


@pytest.mark.asyncio
async def test_add_then_update_no_duplicate(store, monkeypatch):
    org, pid = store.resolve_project("perA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    monkeypatch.setenv("DLP_MEMORY__ENABLED", "true")
    get_config.cache_clear()
    cfg = get_config()

    # First pass — resolver says ADD.
    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)
    f1 = _finding(pid, "Tavily pricing", {"k": "free 1000/mo"})
    out1 = await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)
    assert len(out1.affected_finding_ids) == 1
    assert store.count_findings(kb) == 1
    fid = out1.affected_finding_ids[0]

    # Second pass — overlapping candidate, resolver says UPDATE the existing one.
    async def _update(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(
                candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=fid, reason="refine"
            )
        ]

    monkeypatch.setattr(persist_mod, "resolve", _update)
    f2 = _finding(pid, "Tavily pricing (updated)", {"k": "free + paid tiers"})
    out2 = await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)
    assert out2.affected_finding_ids == [fid]   # same id (stable)
    assert store.count_findings(kb) == 1        # NO duplicate row
    assert store.get_finding(kb, fid)["title"] == "Tavily pricing (updated)"
    evs = store.list_resolution_events(kb)
    assert any(e["op"] == "UPDATE" for e in evs)
    get_config.cache_clear()


@pytest.mark.asyncio
async def test_disabled_is_pure_add(store, monkeypatch):
    org, pid = store.resolve_project("perB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)

    async def _fake_embed(texts):
        return [[0.02] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)

    async def _boom(*a, **k):
        raise AssertionError("resolver must not run when memory.enabled is false")

    monkeypatch.setattr(persist_mod, "resolve", _boom)

    monkeypatch.setenv("DLP_MEMORY__ENABLED", "false")
    get_config.cache_clear()
    cfg = get_config()
    f = _finding(pid, "X", {"k": "v"})
    out = await persist_mod.resolve_and_persist(ctx, store, [f], cfg)
    assert store.count_findings(kb) == 1
    assert len(out.affected_finding_ids) == 1
    assert store.list_resolution_events(kb) == []  # disabled path logs nothing
    get_config.cache_clear()


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty(store):
    org, pid = store.resolve_project("perC", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    get_config.cache_clear()
    out = await persist_mod.resolve_and_persist(ctx, store, [], get_config())
    assert out.affected_finding_ids == []
    assert store.count_findings(kb) == 0
