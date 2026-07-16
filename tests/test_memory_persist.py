from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.config import get_config
from delapan.core.exploration.models import Finding
from delapan.core.memory import persist as persist_mod
from delapan.core.memory.models import ResolutionDecision, ResolutionOp


def _finding(pid, title, content):
    return Finding(exploration_id="e", project_id=pid, category="fact", title=title, content=content)


async def _decisions(ds):
    return ds


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
    new_fid = out2.affected_finding_ids[0]
    assert new_fid != fid                       # UPDATE now versions the row
    assert store.count_findings(kb) == 1        # NO duplicate LIVE row
    assert store.get_finding(kb, fid)["superseded_by"] == new_fid
    assert store.get_finding(kb, new_fid)["title"] == "Tavily pricing (updated)"
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


async def _cfg_with_memory(monkeypatch):
    from delapan.core.config import get_config

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    get_config.cache_clear()
    cfg = get_config()
    cfg.memory.enabled = True
    return cfg


@pytest.mark.asyncio
async def test_noop_corroborates_target_and_raises_confidence(store, monkeypatch):
    org, pid = store.resolve_project("noopA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    f1 = _finding(pid, "Tavily pricing", {"k": "free 1000/mo"})
    f1.provenance = [{"url": "http://a"}]
    fid = (await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)).affected_finding_ids[0]
    before_row = store.get_finding(kb, fid)
    before, before_content = before_row["confidence"], before_row["content"]

    # A duplicate citing a NEW url → corroboration: same row, more sources.
    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP,
                               target_finding_id=fid, reason="same fact")
        ]),
    )
    f2 = _finding(pid, "Tavily pricing", {"k": "free 1000/mo"})
    f2.provenance = [{"url": "http://b"}]
    out = await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)

    assert out.affected_finding_ids == []          # NOOP touches no new row
    assert store.count_findings(kb) == 1           # no duplicate
    row = store.get_finding(kb, fid)
    assert {p["url"] for p in row["provenance"]} == {"http://a", "http://b"}
    assert row["confidence"] > before               # monotonic raise
    assert row["content"] == before_content         # body untouched — NOOP never rewrites content


@pytest.mark.asyncio
async def test_noop_with_no_new_url_is_a_true_no_write(store, monkeypatch):
    org, pid = store.resolve_project("noopB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    f1 = _finding(pid, "T", {"k": "v"})
    f1.provenance = [{"url": "http://a"}]
    fid = (await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)).affected_finding_ids[0]
    snapshot = store.get_finding(kb, fid)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP, target_finding_id=fid)
        ]),
    )
    f2 = _finding(pid, "T", {"k": "v"})
    f2.provenance = [{"url": "http://a"}]          # same url — nothing to corroborate
    await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)

    assert store.get_finding(kb, fid) == snapshot  # byte-identical row
    assert store.list_resolution_events(kb)[0]["op"] == "NOOP"  # but still audited


@pytest.mark.asyncio
async def test_supersede_retires_contradicted_row_without_deleting(store, monkeypatch):
    org, pid = store.resolve_project("supA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    old = _finding(pid, "Tavily is free", {"k": "free forever"})
    old.provenance = [{"url": "http://old"}]
    old_id = (await persist_mod.resolve_and_persist(ctx, store, [old], cfg)).affected_finding_ids[0]

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.SUPERSEDE,
                               target_finding_id=old_id, reason="contradicts")
        ]),
    )
    new = _finding(pid, "Tavily is paid", {"k": "$30/mo"})
    new.provenance = [{"url": "http://new"}]
    out = await persist_mod.resolve_and_persist(ctx, store, [new], cfg)

    new_id = out.affected_finding_ids[0]
    assert new_id != old_id
    assert store.count_findings(kb) == 1                     # old retired, new live
    retired = store.get_finding(kb, old_id)                  # NOT deleted
    assert retired["superseded_by"] == new_id
    # A contradicted finding's sources must not corroborate its contradiction.
    assert {p["url"] for p in store.get_finding(kb, new_id)["provenance"]} == {"http://new"}
    assert store.list_resolution_events(kb)[0]["op"] == "SUPERSEDE"


@pytest.mark.asyncio
async def test_update_versions_the_row_and_merges_provenance(store, monkeypatch):
    org, pid = store.resolve_project("updA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    f1 = _finding(pid, "Pricing", {"k": "old detail"})
    f1.provenance = [{"url": "http://a"}]
    old_id = (await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)).affected_finding_ids[0]

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=old_id)
        ]),
    )
    f2 = _finding(pid, "Pricing", {"k": "refined detail"})
    f2.provenance = [{"url": "http://b"}]
    new_id = (await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)).affected_finding_ids[0]

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, old_id)["superseded_by"] == new_id
    new_row = store.get_finding(kb, new_id)
    assert {p["url"] for p in new_row["provenance"]} == {"http://a", "http://b"}  # union
    assert new_row["confidence"] > 0.4                                # two sources


@pytest.mark.asyncio
async def test_noop_with_vanished_target_falls_back_to_add(store, monkeypatch):
    org, pid = store.resolve_project("noopC", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    # Resolver claims a NOOP duplicate against a target that no longer exists
    # (e.g. retired by another writer between resolve() and apply()).
    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP,
                                target_finding_id="ghost-id", reason="dup?")
        ]),
    )
    f = _finding(pid, "Orphaned NOOP candidate", {"k": "still worth keeping"})
    out = await persist_mod.resolve_and_persist(ctx, store, [f], cfg)

    assert len(out.affected_finding_ids) == 1      # candidate was persisted, not dropped
    assert store.count_findings(kb) == 1
    assert len(out.events) == 1
    assert out.events[0].op == "ADD"               # not a bare NOOP-skip event
    assert "noop target vanished" in out.events[0].reason


@pytest.mark.asyncio
async def test_failing_supersede_finding_falls_back_to_add(store, monkeypatch):
    org, pid = store.resolve_project("updB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    old = _finding(pid, "Pricing", {"k": "old detail"})
    old.provenance = [{"url": "http://a"}]
    old_id = (await persist_mod.resolve_and_persist(ctx, store, [old], cfg)).affected_finding_ids[0]

    # Target is still live at the initial get_finding check, but the
    # supersede_finding write itself fails (narrow race: retired concurrently).
    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=old_id)
        ]),
    )

    async def _boom(*a, **k):
        raise RuntimeError("supersede target not live")

    monkeypatch.setattr(store, "supersede_finding", _boom)

    f2 = _finding(pid, "Pricing", {"k": "refined detail"})
    f2.provenance = [{"url": "http://b"}]
    out = await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)

    assert len(out.affected_finding_ids) == 1      # candidate was persisted, not dropped
    assert store.count_findings(kb) == 2           # old (never retired) + new ADD row
    assert store.get_finding(kb, old_id)["superseded_by"] is None  # untouched — write never applied
    assert len(out.events) == 1
    assert out.events[0].op == "ADD"               # not a bare warning+drop
    assert "failed" in out.events[0].reason


