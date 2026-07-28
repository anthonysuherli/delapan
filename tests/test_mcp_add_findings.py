from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.memory.models import ResolutionDecision, ResolutionOp, ResolutionOutcome
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
        return [
            ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))
        ]

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
async def test_rejects_when_no_embedding_credential(ctx_and_store, monkeypatch):
    """Keyless install: a clean error before any store mutation, never a raw
    MissingEmbeddingKeyError traceback surfacing from embed_batch deep in the
    try block."""
    ctx, store = ctx_and_store
    monkeypatch.setattr(
        server_mod,
        "get_settings",
        lambda: SimpleNamespace(ai_gateway_api_key=None, openai_api_key=None),
    )
    out = await server_mod._add_findings_impl(ctx, [_raw("keyless")])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_oversized_batch(ctx_and_store, monkeypatch):
    """AI spend is bounded and observable by construction — the same cap explore
    enforces applies to agent-submitted batches, so nothing gets embedded or
    routed through the resolver before the check runs."""
    ctx, store = ctx_and_store
    monkeypatch.setenv("DLP_EXPLORATION__MAX_FINDINGS", "2")
    from delapan.core.config import get_config

    get_config.cache_clear()
    try:
        out = await server_mod._add_findings_impl(ctx, [_raw("a"), _raw("b"), _raw("c")])
        assert "error" in out
        assert "3" in out["error"]
        assert "2" in out["error"]
        assert store.count_findings(ctx.kb_id) == 0
    finally:
        get_config.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_confidence", ["high", None, [0.5], True])
async def test_rejects_bad_confidence_type(ctx_and_store, bad_confidence):
    ctx, store = ctx_and_store
    bad = _raw("bad confidence type")
    bad["confidence"] = bad_confidence
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert "confidence" in out["error"]
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_confidence", [85, -0.1, 1.5])
async def test_rejects_out_of_range_confidence(ctx_and_store, bad_confidence):
    """confidence: 85 is the classic 0-100 vs 0-1 agent slip — it must not
    persist and silently outrank every verified finding."""
    ctx, store = ctx_and_store
    bad = _raw("out of range confidence")
    bad["confidence"] = bad_confidence
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert "confidence" in out["error"]
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_non_dict_content(ctx_and_store):
    ctx, store = ctx_and_store
    bad = _raw("non-dict content")
    bad["content"] = "a string"
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert "content" in out["error"]
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_non_string_title(ctx_and_store):
    ctx, store = ctx_and_store
    bad = _raw("placeholder")
    bad["title"] = 12345
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert "title" in out["error"]
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_all_duplicates_returns_note(ctx_and_store, monkeypatch):
    """Every finding resolves as a duplicate — mirrors _explore_impl's peer
    behavior so an agent doesn't resubmit against a bare count:0 and buy more
    resolver calls."""
    ctx, _store = ctx_and_store

    async def _empty_outcome(ctx_, store_, candidates, cfg):
        return ResolutionOutcome(affected_finding_ids=[])

    monkeypatch.setattr(server_mod, "resolve_and_persist", _empty_outcome)
    out = await server_mod._add_findings_impl(ctx, [_raw("dup")])
    assert out["status"] == "completed"
    assert out["count"] == 0
    assert out["finding_ids"] == []
    assert "note" in out
    assert "duplicate" in out["note"]


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


@pytest.mark.asyncio
async def test_reingest_updates_instead_of_duplicating(store, monkeypatch):
    """Second submission of the same claim resolves as UPDATE — no duplicate row."""
    org, pid = store.resolve_project("dedupproj", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid, access_token=None)

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    async def _no_synopsis(kb_id, org_id=None, store=None):
        return "skipped"

    monkeypatch.setattr("delapan.core.memory.persist.embed_batch", _fake_embed)
    monkeypatch.setattr(server_mod, "maybe_rebuild_synopsis", _no_synopsis)
    monkeypatch.setattr(server_mod, "schedule_kg_update", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "get_store", lambda *a, **k: store)

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))
        ]

    monkeypatch.setattr("delapan.core.memory.persist.resolve", _all_add)
    monkeypatch.setenv("DLP_MEMORY__ENABLED", "true")
    from delapan.core.config import get_config

    get_config.cache_clear()
    try:
        out = await server_mod._add_findings_impl(ctx, [_raw("gateway is zero markup")])
        assert store.count_findings(kb) == 1
        existing_id = out["finding_ids"][0]

        # Second pass — resolver says UPDATE against the existing row.
        async def _all_update(store_, kb_, cands, embs, mcfg):
            return [
                ResolutionDecision(
                    candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=existing_id
                )
            ]

        monkeypatch.setattr("delapan.core.memory.persist.resolve", _all_update)
        await server_mod._add_findings_impl(ctx, [_raw("gateway is zero markup")])

        assert store.count_findings(kb) == 1, "UPDATE must not create a second row"
    finally:
        get_config.cache_clear()
