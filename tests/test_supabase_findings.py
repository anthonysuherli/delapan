import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


@pytest.mark.asyncio
async def test_insert_sets_org_and_status_and_vec(monkeypatch):
    store, fake = make_store(monkeypatch)
    ids = await store.insert_findings([
        {"id": "f1", "kb_id": "kb1", "title": "T", "content": "body",
         "category": "c", "confidence": 0.9, "tags": ["a"],
         "provenance": [{"url": "u"}], "embedding": [0.1, 0.2]},
    ])
    assert ids == ["f1"]
    row = fake.tables["findings"][0]
    assert row["org_id"] == "org1" and row["status"] == "approved"
    assert row["embedding"] == "[0.1,0.2]"
    assert row["tags"] == ["a"] and row["provenance"] == [{"url": "u"}]


def test_get_finding_raises_when_absent(monkeypatch):
    store, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        store.get_finding("kb1", "nope")


def test_get_finding_global_ignores_kb(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [{"id": "f1", "org_id": "org1", "kb_id": "other",
                                "title": "T", "content": "b", "category": "c",
                                "confidence": 0.5, "tags": [], "provenance": [],
                                "created_at": "t"}]
    got = store.get_finding_global("f1")
    assert got["id"] == "f1" and got["title"] == "T" and "created_at" in got


def test_list_and_count(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": f"f{i}", "org_id": "org1", "kb_id": "kb1", "title": f"T{i}",
         "category": "c", "confidence": 0.5, "tags": [], "created_at": f"t{i}"}
        for i in range(3)
    ]
    out = store.list_findings("kb1")
    assert out["count"] == 3 and out["findings"][0]["id"].startswith("f")
    assert store.count_findings("kb1") == 3


def test_list_total_reflects_full_match_count_despite_limit(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": f"f{i}", "org_id": "org1", "kb_id": "kb1", "title": f"T{i}",
         "category": "c", "confidence": 0.5, "tags": [], "created_at": f"t{i}"}
        for i in range(5)
    ]
    out = store.list_findings("kb1", limit=2)
    assert out["count"] == 2
    assert len(out["findings"]) == 2
    assert out["total"] == 5


def test_list_total_respects_category_filter(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": f"a{i}", "org_id": "org1", "kb_id": "kb1", "title": f"A{i}",
         "category": "alpha", "confidence": 0.5, "tags": [], "created_at": f"t{i}"}
        for i in range(4)
    ] + [
        {"id": f"b{i}", "org_id": "org1", "kb_id": "kb1", "title": f"B{i}",
         "category": "beta", "confidence": 0.5, "tags": [], "created_at": f"u{i}"}
        for i in range(2)
    ]
    out = store.list_findings("kb1", category="alpha", limit=1)
    assert out["count"] == 1
    assert len(out["findings"]) == 1
    assert out["total"] == 4


@pytest.mark.asyncio
async def test_match_findings_calls_rpc_with_match_kb_id(monkeypatch):
    store, fake = make_store(monkeypatch)
    seen = {}
    fake.register_rpc("match_findings", lambda p: (seen.update(p) or [
        {"id": "f1", "title": "T", "content": "b", "category": "c",
         "confidence": 0.9, "tags": [], "provenance": [], "similarity": 0.8}]))
    hits = await store.match_findings("kb1", [0.1, 0.2], match_count=5, min_similarity=0.0)
    assert seen["match_kb_id"] == "kb1" and seen["query_embedding"] == "[0.1,0.2]"
    assert hits[0]["similarity"] == 0.8
