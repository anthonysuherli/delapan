"""Acceptance #3 (idempotence): re-running one candidate batch through
``resolve_and_persist`` a second time — with a stubbed resolver that now
recognizes the candidates as duplicates — must be a true no-write.

    pass 1: resolve() → ADD, ADD        ─► 2 new live rows
    pass 2: resolve() → NOOP, NOOP      ─► 0 ADDs, same live count, rows unchanged

Mirrors the patterns in tests/test_memory_persist.py (monkeypatch embed_batch
and resolve, use the `store` fixture from tests/conftest.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.config import get_config
from delapan.core.exploration.models import Finding
from delapan.core.memory import persist as persist_mod
from delapan.core.memory.models import ResolutionDecision, ResolutionOp


def _finding(pid, title, content, url):
    f = Finding(exploration_id="e", project_id=pid, category="fact", title=title, content=content)
    f.provenance = [{"url": url}]
    return f


async def _cfg_with_memory(monkeypatch):
    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    monkeypatch.setattr(persist_mod, "active_backend", lambda: "local")
    get_config.cache_clear()
    cfg = get_config()
    cfg.memory.enabled = True
    return cfg


@pytest.mark.asyncio
async def test_second_pass_with_noop_resolver_is_a_true_no_write(store, monkeypatch):
    org, pid = store.resolve_project("idem", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    batch = [
        _finding(pid, "Tavily pricing", {"k": "free 1000/mo"}, "http://a"),
        _finding(pid, "Rate limits", {"k": "60 rpm"}, "http://b"),
    ]

    # Pass 1 — resolver says ADD for both candidates.
    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)
    out1 = await persist_mod.resolve_and_persist(ctx, store, batch, cfg)
    assert len(out1.affected_finding_ids) == 2
    live_before = store.count_findings(kb)
    assert live_before == 2

    fid_a, fid_b = out1.affected_finding_ids

    # Attribution: fid_a/fid_b must map to the FIRST/SECOND candidate respectively,
    # not just "some id from the batch" — the add_events/new_ids zip in persist.py
    # is positional, and a 2-item batch is the smallest case where misalignment
    # would show up (a 1-item batch is trivially "aligned").
    assert store.get_finding(kb, fid_a)["title"] == batch[0].title
    assert store.get_finding(kb, fid_b)["title"] == batch[1].title

    # Cross-check via the resolution-event log: each ADD event's new_finding_id
    # must map back to the same candidate_title, directly proving the
    # add_events/new_ids zip stayed aligned for this batch.
    events_by_new_id = {
        e["new_finding_id"]: e["candidate_title"] for e in store.list_resolution_events(kb)
    }
    assert events_by_new_id[fid_a] == batch[0].title
    assert events_by_new_id[fid_b] == batch[1].title

    snapshot_a = store.get_finding(kb, fid_a)
    snapshot_b = store.get_finding(kb, fid_b)

    # Pass 2 — same batch (same content, same urls — nothing new to corroborate),
    # stubbed resolver now recognizes both as duplicates of the rows just inserted.
    async def _all_noop(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP, target_finding_id=fid_a),
            ResolutionDecision(candidate_index=1, op=ResolutionOp.NOOP, target_finding_id=fid_b),
        ]

    monkeypatch.setattr(persist_mod, "resolve", _all_noop)
    out2 = await persist_mod.resolve_and_persist(ctx, store, batch, cfg)

    assert out2.affected_finding_ids == []  # 0 ADDs on the second pass
    assert all(e.op == "NOOP" for e in out2.events)

    live_after = store.count_findings(kb)
    assert live_after == live_before  # count_findings stable between passes

    # No new url → nothing to corroborate → byte-identical rows (true no-write).
    assert store.get_finding(kb, fid_a) == snapshot_a
    assert store.get_finding(kb, fid_b) == snapshot_b

    get_config.cache_clear()
