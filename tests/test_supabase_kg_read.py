import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


def _node(i, kb="kb1"):
    return {"id": i, "org_id": "org1", "kb_id": kb, "type": "concept", "label": i,
            "properties": {}, "grounded_in": [], "created_at": "t"}


def _edge(eid, s, t, kb="kb1"):
    return {"id": eid, "org_id": "org1", "kb_id": kb, "source_node_id": s,
            "target_node_id": t, "relation": "r", "properties": {},
            "grounded_in": [], "created_at": "t"}


def test_full_subgraph(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [_node("a"), _node("b")]
    fake.tables["kg_edges"] = [_edge("e1", "a", "b")]
    g = store.get_kg_subgraph("kb1")
    assert {n["id"] for n in g["nodes"]} == {"a", "b"}
    assert g["edges"][0]["source_node_id"] == "a"


def test_bfs_subgraph_one_hop(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [_node("a"), _node("b"), _node("c")]
    fake.tables["kg_edges"] = [_edge("e1", "a", "b"), _edge("e2", "b", "c")]
    g = store.get_kg_subgraph("kb1", seed_node_ids=["a"], depth=1)
    assert "a" in {n["id"] for n in g["nodes"]}
    assert "b" in {n["id"] for n in g["nodes"]}  # one hop reaches b


def test_kg_stats(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [_node("a"), {**_node("b"), "type": "tech"}]
    fake.tables["kg_edges"] = [_edge("e1", "a", "b")]
    s = store.kg_stats("kb1")
    assert s["node_count"] == 2 and s["edge_count"] == 1
    assert s["by_type"] == {"concept": 1, "tech": 1}


def test_get_kg_node_none_when_absent(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.get_kg_node("kb1", "nope") is None


@pytest.mark.asyncio
async def test_match_kg_nodes_rpc(monkeypatch):
    store, fake = make_store(monkeypatch)
    seen = {}
    fake.register_rpc("match_kg_nodes", lambda p: (seen.update(p) or [
        {"id": "a", "type": "concept", "label": "A", "properties": {}, "similarity": 1.0}]))
    hits = await store.match_kg_nodes("kb1", [0.1], match_count=3, min_similarity=0.0)
    assert seen["match_kb_id"] == "kb1" and hits[0]["similarity"] == 1.0
