import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


@pytest.mark.asyncio
async def test_upsert_nodes_inserts_with_required_cols(monkeypatch):
    store, fake = make_store(monkeypatch)
    ids = await store.upsert_kg_nodes("kb1", [
        {"type": "concept", "label": "A", "properties": {"x": 1},
         "grounded_in": ["f1"], "embedding": [0.1]},
    ])
    assert len(ids) == 1
    row = fake.tables["kg_nodes"][0]
    assert row["org_id"] == "org1" and row["aliases"] == [] and row["merge_history"] == []
    assert row["embedding"] == "[0.1]"


@pytest.mark.asyncio
async def test_upsert_nodes_merges_existing(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [{"id": "n1", "org_id": "org1", "kb_id": "kb1",
                                "type": "concept", "label": "A",
                                "properties": {"keep": 1}, "grounded_in": ["f1"],
                                "aliases": [], "merge_history": [], "created_at": "t"}]
    ids = await store.upsert_kg_nodes("kb1", [
        {"type": "concept", "label": "A", "properties": {"keep": 9, "new": 2},
         "grounded_in": ["f2"]}])
    assert ids == ["n1"]  # reused
    row = fake.tables["kg_nodes"][0]
    assert row["properties"]["keep"] == 1  # existing wins
    assert row["grounded_in"] == ["f1", "f2"]  # unioned


@pytest.mark.asyncio
async def test_upsert_edges_skips_dupes_and_selfloops(monkeypatch):
    store, _fake = make_store(monkeypatch)
    n = await store.upsert_kg_edges("kb1", [
        {"source_node_id": "a", "target_node_id": "b", "relation": "r"},
        {"source_node_id": "a", "target_node_id": "b", "relation": "r"},  # dupe
        {"source_node_id": "a", "target_node_id": "a", "relation": "r"},  # self
    ])
    assert n == 1


def test_delete_node_removes_incident_edges(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [{"id": "a", "org_id": "org1", "kb_id": "kb1",
                                "type": "c", "label": "A", "properties": {},
                                "grounded_in": [], "created_at": "t"}]
    fake.tables["kg_edges"] = [{"id": "e1", "org_id": "org1", "kb_id": "kb1",
                                "source_node_id": "a", "target_node_id": "b",
                                "relation": "r", "properties": {}, "grounded_in": [],
                                "created_at": "t"}]
    res = store.delete_kg_node("kb1", "a")
    assert res["deleted"] is True and res["removed_edge_ids"] == ["e1"]
    assert fake.tables["kg_nodes"] == [] and fake.tables["kg_edges"] == []


def test_delete_missing_node(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.delete_kg_node("kb1", "nope") == {"deleted": False, "removed_edge_ids": []}
