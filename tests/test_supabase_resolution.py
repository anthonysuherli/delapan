"""SupabaseStore resolution-write-primitive tests — mirrors test_supabase_findings.py's
fake-client pattern (FakeSupabase, monkeypatch on `user_client`, no network)."""

from __future__ import annotations

import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


@pytest.mark.asyncio
async def test_update_finding_omits_none_fields(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": "f1", "kb_id": "kb1", "org_id": "org1", "title": "T",
         "content": "keep", "confidence": 0.4, "provenance": []}
    ]
    await store.update_finding("kb1", "f1", confidence=0.64, provenance=[{"url": "http://b"}])
    row = fake.tables["findings"][0]
    assert row["content"] == "keep"  # None → not in the payload at all
    assert row["confidence"] == 0.64
    assert row["provenance"] == [{"url": "http://b"}]


@pytest.mark.asyncio
async def test_update_finding_noop_when_all_none(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [{"id": "f1", "kb_id": "kb1", "content": "keep"}]
    await store.update_finding("kb1", "f1")
    assert fake.tables["findings"][0]["content"] == "keep"


@pytest.mark.asyncio
async def test_invalidate_finding_stamps_pointer(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": "f1", "kb_id": "kb1", "org_id": "org1",
         "invalidated_at": None, "superseded_by": None}
    ]
    await store.invalidate_finding("kb1", "f1", superseded_by="f2")
    row = fake.tables["findings"][0]
    assert row["invalidated_at"]
    assert row["superseded_by"] == "f2"


@pytest.mark.asyncio
async def test_supersede_calls_rpc_with_enriched_row(monkeypatch):
    store, fake = make_store(monkeypatch)
    captured = {}
    fake.register_rpc("supersede_finding", lambda params: captured.update(params) or "new1")
    new_id = await store.supersede_finding(
        "kb1", "old1",
        {"kb_id": "kb1", "title": "T", "content": "c", "category": "fact",
         "confidence": 0.6, "tags": [], "provenance": [{"url": "http://a"}],
         "embedding": [0.1] * 4},
    )
    assert new_id == "new1"
    row = captured["p_row"]
    assert row["org_id"] == "org1"  # enriched like insert_findings
    assert row["status"] == "approved"
    assert row["valid_from"]
    assert row["embedding"].startswith("[")  # vector text, not a list
    assert captured["p_kb_id"] == "kb1"
    assert captured["p_target_id"] == "old1"


def test_count_and_list_hide_invalidated(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": "live", "kb_id": "kb1", "org_id": "org1", "invalidated_at": None,
         "title": "A", "content": "x", "category": "fact", "confidence": 0.5,
         "tags": [], "created_at": "2026-01-01"},
        {"id": "dead", "kb_id": "kb1", "org_id": "org1", "invalidated_at": "2026-07-16",
         "title": "B", "content": "y", "category": "fact", "confidence": 0.5,
         "tags": [], "created_at": "2026-01-02"},
    ]
    assert store.count_findings("kb1") == 1
    assert [f["id"] for f in store.list_findings("kb1")["findings"]] == ["live"]
    assert len(store.list_findings("kb1", include_invalidated=True)["findings"]) == 2


@pytest.mark.asyncio
async def test_insert_and_list_resolution_events(monkeypatch):
    store, fake = make_store(monkeypatch)
    await store.insert_resolution_events("kb1", [
        {"op": "ADD", "candidate_title": "T1", "new_finding_id": "f1"},
        {"op": "NOOP", "candidate_title": "T2", "target_finding_id": "f1",
         "details": {"merged_urls": ["http://a"]}, "reason": "corroborated"},
    ])
    rows = fake.tables["resolution_events"]
    assert len(rows) == 2
    assert all(r["org_id"] == "org1" and r["kb_id"] == "kb1" for r in rows)
    events = store.list_resolution_events("kb1")
    assert len(events) == 2
    assert {e["op"] for e in events} == {"ADD", "NOOP"}


@pytest.mark.asyncio
async def test_insert_resolution_events_noop_on_empty(monkeypatch):
    store, fake = make_store(monkeypatch)
    await store.insert_resolution_events("kb1", [])
    assert "resolution_events" not in fake.tables
