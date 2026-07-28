from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.memory.models import ResolutionDecision, ResolutionOp
from delapan.mcp import server as server_mod


def _raw(title: str, url: str = "https://example.com/a") -> dict:
    return {
        "title": title,
        "category": "fact",
        "content": {"claim": f"{title} body"},
        "provenance": [{"url": url}],
    }


@pytest.fixture
def ctx_and_store(store, monkeypatch):
    org, pid = store.resolve_project("agentproj", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid, access_token=None)

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    async def _no_synopsis(kb_id, org_id=None, store=None):
        return "skipped"

    monkeypatch.setattr("delapan.core.memory.persist.embed_batch", _fake_embed)
    monkeypatch.setattr("delapan.core.memory.persist.resolve", _all_add)
    monkeypatch.setattr(server_mod, "maybe_rebuild_synopsis", _no_synopsis)
    monkeypatch.setattr(server_mod, "schedule_kg_update", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "get_store", lambda *a, **k: store)
    return ctx, store


@pytest.mark.asyncio
async def test_rejects_finding_without_provenance(ctx_and_store):
    """Grounding is a precondition of the tool, not a caller convention."""
    ctx, store = ctx_and_store
    bad = {"title": "ungrounded", "category": "fact", "content": {"claim": "x"}}
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert "provenance" in out["error"]
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_empty_provenance_list(ctx_and_store):
    ctx, store = ctx_and_store
    bad = _raw("empty prov")
    bad["provenance"] = []
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_provenance_with_empty_dict(ctx_and_store):
    """[{}] is non-empty as a list but carries no url — must not slip past validation."""
    ctx, store = ctx_and_store
    bad = _raw("empty dict prov")
    bad["provenance"] = [{}]
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_provenance_with_whitespace_url(ctx_and_store):
    ctx, store = ctx_and_store
    bad = _raw("whitespace url prov")
    bad["provenance"] = [{"url": "   "}]
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", [None, False, 0])
async def test_rejects_provenance_with_null_url(ctx_and_store, bad_url):
    """{'url': None} (and other non-string falsy values) must not slip past
    validation via str() coercion — str(None) == 'None' is truthy after strip()."""
    ctx, store = ctx_and_store
    bad = _raw("null url prov")
    bad["provenance"] = [{"url": bad_url}]
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_non_list_provenance(ctx_and_store):
    ctx, store = ctx_and_store
    bad = _raw("non-list prov")
    bad["provenance"] = "https://example.com/a"
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_empty_batch(ctx_and_store):
    ctx, _store = ctx_and_store
    out = await server_mod._add_findings_impl(ctx, [])
    assert "error" in out


@pytest.mark.asyncio
async def test_persists_grounded_findings(ctx_and_store):
    """Happy path: findings land and ids come back."""
    ctx, store = ctx_and_store
    out = await server_mod._add_findings_impl(ctx, [_raw("alpha"), _raw("beta")])
    assert out["status"] == "completed"
    assert out["count"] == 2
    assert len(out["finding_ids"]) == 2
    assert store.count_findings(ctx.kb_id) == 2


@pytest.mark.asyncio
async def test_provenance_survives_persistence(ctx_and_store):
    """The grounded_in Invariant: the source url must still be there after the write."""
    ctx, store = ctx_and_store
    url = "https://vercel.com/docs/ai-gateway/pricing"
    out = await server_mod._add_findings_impl(ctx, [_raw("zero markup", url=url)])

    assert len(out["finding_ids"]) == 1
    row = store.get_finding(ctx.kb_id, out["finding_ids"][0])
    assert url in [p.get("url") for p in row["provenance"]], (
        "provenance url was dropped between the tool and the store"
    )
