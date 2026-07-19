from __future__ import annotations

import pytest

from tests.test_supabase_misc import make_store  # same fake-client harness

EMB = [0.1] * 1536


@pytest.mark.asyncio
async def test_record_access_matches_live_column_set(monkeypatch):
    """Regression test for the schema bug: the insert must carry target_type and
    must NOT carry targets/created_at (neither column exists on the live table)."""
    store, fake = make_store(monkeypatch)
    await store.record_access(
        org_id="ignored", kb_id="kb1", surface="resume", targets=["f1", "f2"],
        query_text="q", coverage="gap", band_counts={"1": 0},
    )
    payload = fake.table("access_events").last_insert
    assert set(payload) == {
        "org_id", "kb_id", "target_type", "target_id", "surface",
        "query_text", "coverage", "band_counts", "ts",
    }
    assert payload["target_type"] == "query"
    assert payload["org_id"] == store._org_id  # store self-scopes; arg ignored
    assert "targets" not in payload and "created_at" not in payload


@pytest.mark.asyncio
async def test_record_access_still_never_raises(monkeypatch):
    store, _ = make_store(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(store._c, "table", boom)
    await store.record_access(
        org_id="o", kb_id="kb1", surface="s", targets=[], coverage="gap"
    )


@pytest.mark.asyncio
async def test_upsert_calls_rpc_with_conventional_params(monkeypatch):
    store, fake = make_store(monkeypatch)
    await store.upsert_curation_topic({
        "org_id": "o", "kb_id": "kb1", "query_text": "what is csm",
        "query_norm": "what is csm", "embedding": EMB, "coverage": "gap",
        "seen_at": "2026-07-16T00:00:00+00:00",
    })
    name, params = fake.last_rpc
    assert name == "upsert_curation_topic"
    assert params["p_org_id"] == store._org_id
    assert params["p_embedding"].startswith("[") and params["p_embedding"].endswith("]")
    assert params["p_query_norm"] == "what is csm"


@pytest.mark.asyncio
async def test_match_calls_rpc_with_deployed_param_names(monkeypatch):
    store, fake = make_store(monkeypatch)
    await store.match_curation_topics("kb1", EMB, 5, 0.8)
    name, params = fake.last_rpc
    assert name == "match_curation_topics"
    assert set(params) == {"query_embedding", "match_kb_id", "match_count", "min_similarity"}
    assert params["match_kb_id"] == "kb1"
