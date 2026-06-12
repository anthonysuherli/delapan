from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp DB with no provider keys (embedding-free paths)."""
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "api.db"))
    for key in ("OPENAI_API_KEY", "TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    from delapan.api.main import app

    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture()
def kb(client):
    """A resolved (store, kb_id) for project 'proj' / kb 'kb' on the tmp DB."""
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("proj", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "kb", create=True)
    return store, kb_id


BASE = "/api/projects/proj/kbs/kb"


def _post_nodes(client, nodes):
    r = client.post(f"{BASE}/graph/nodes", json={"nodes": nodes})
    assert r.status_code == 200
    return r.json()["ids"]


def test_unknown_project_or_kb_is_404(client):
    assert client.get("/api/projects/nope/kbs/kb/graph").status_code == 404
    assert client.get("/api/projects/nope/kbs/kb/findings").status_code == 404
    assert client.get("/api/projects/nope/kbs/kb/graph/stats").status_code == 404
    assert client.post("/api/projects/nope/kbs/kb/explore", json={"prompt": "x"}).status_code == 404


def test_projects_listing(client, kb):
    r = client.get("/api/projects")
    assert r.status_code == 200
    projects = r.json()["projects"]
    proj = next(p for p in projects if p["project"] == "proj")
    assert proj["kbs"][0]["kb"] == "kb" and proj["kbs"][0]["kb_id"]


def test_graph_read_stats_schema_after_seeding(client, kb):
    store, kb_id = kb
    ids = _post_nodes(
        client,
        [
            {"type": "Concept", "label": "Alpha", "properties": {"k": "v"}},
            {"type": "System", "label": "Beta"},
        ],
    )
    r = client.post(
        f"{BASE}/graph/edges",
        json={"edges": [{"source": ids[0], "target": ids[1], "relation": "uses"}]},
    )
    assert r.status_code == 200 and r.json() == {"inserted": 1}

    graph = client.get(f"{BASE}/graph").json()
    assert {n["id"] for n in graph["nodes"]} == set(ids)
    node = graph["nodes"][0]
    assert set(node) == {"id", "type", "label", "properties", "grounded_in", "created_at"}
    edge = graph["edges"][0]
    assert set(edge) == {
        "id",
        "source",
        "target",
        "relation",
        "properties",
        "grounded_in",
        "created_at",
    }
    assert edge["source"] == ids[0] and edge["target"] == ids[1] and edge["relation"] == "uses"

    stats = client.get(f"{BASE}/graph/stats").json()
    assert stats["node_count"] == 2 and stats["edge_count"] == 1
    assert stats["by_type"] == {"Concept": 1, "System": 1} and stats["by_relation"] == {"uses": 1}

    schema = client.get(f"{BASE}/graph/schema").json()
    assert schema["intent"] is None
    assert schema["emergent"]["node_types"] == ["Concept", "System"]
    store.set_kg_intent("local", kb_id, {"node_types": ["Concept"]})
    schema = client.get(f"{BASE}/graph/schema").json()
    assert schema["intent"]["node_types"] == ["Concept"] and schema["intent"]["version"] == 1


def test_node_patch_renames_and_updates(client, kb):
    (node_id,) = _post_nodes(client, [{"type": "Concept", "label": "Old", "properties": {"a": 1}}])
    r = client.patch(
        f"{BASE}/graph/nodes/{node_id}",
        json={"label": "New", "type": "System", "properties": {"b": 2}},
    )
    assert r.status_code == 200
    node = r.json()["node"]
    assert node["label"] == "New" and node["type"] == "System" and node["properties"] == {"b": 2}

    graph = client.get(f"{BASE}/graph").json()
    assert graph["nodes"][0]["label"] == "New"
    assert client.patch(f"{BASE}/graph/nodes/missing", json={"label": "x"}).status_code == 404


def test_node_delete_removes_incident_edges(client, kb):
    ids = _post_nodes(
        client,
        [
            {"type": "Concept", "label": "A"},
            {"type": "Concept", "label": "B"},
            {"type": "Concept", "label": "C"},
        ],
    )
    r = client.post(
        f"{BASE}/graph/edges",
        json={
            "edges": [
                {"source": ids[0], "target": ids[1], "relation": "r1"},
                {"source": ids[1], "target": ids[2], "relation": "r2"},
            ]
        },
    )
    assert r.json() == {"inserted": 2}

    r = client.delete(f"{BASE}/graph/nodes/{ids[1]}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True and len(body["removed_edge_ids"]) == 2

    stats = client.get(f"{BASE}/graph/stats").json()
    assert stats["node_count"] == 2 and stats["edge_count"] == 0
    assert client.delete(f"{BASE}/graph/nodes/{ids[1]}").status_code == 404


def test_edge_post_then_delete(client, kb):
    ids = _post_nodes(client, [{"type": "T", "label": "X"}, {"type": "T", "label": "Y"}])
    client.post(
        f"{BASE}/graph/edges",
        json={"edges": [{"source": ids[0], "target": ids[1], "relation": "rel"}]},
    )
    edge_id = client.get(f"{BASE}/graph").json()["edges"][0]["id"]
    r = client.delete(f"{BASE}/graph/edges/{edge_id}")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert client.get(f"{BASE}/graph/stats").json()["edge_count"] == 0
    assert client.delete(f"{BASE}/graph/edges/{edge_id}").status_code == 404


def test_findings_list_get_delete(client, kb):
    store, kb_id = kb
    asyncio.run(
        store.insert_findings(
            [
                {
                    "id": "f1",
                    "kb_id": kb_id,
                    "title": "T1",
                    "content": {"summary": "body text"},
                    "category": "fact",
                    "confidence": 0.8,
                    "tags": ["t"],
                    "provenance": [{"url": "http://example.com"}],
                }
            ]
        )
    )
    listing = client.get(f"{BASE}/findings").json()
    assert listing["count"] == 1 and listing["findings"][0]["title"] == "T1"
    assert client.get(f"{BASE}/findings?category=other").json()["count"] == 0

    full = client.get(f"{BASE}/findings/f1").json()
    assert full["content"] == {"summary": "body text"}
    assert full["provenance"] == [{"url": "http://example.com"}]

    r = client.delete(f"{BASE}/findings/f1")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert client.get(f"{BASE}/findings/f1").status_code == 404
    assert client.delete(f"{BASE}/findings/f1").status_code == 404


def test_synopsis_null_then_row(client, kb):
    store, kb_id = kb
    assert client.get(f"{BASE}/synopsis").json() is None
    store.upsert_synopsis(kb_id, content=[{"topic": "t", "gloss": "g"}], finding_count=1, model="m")
    row = client.get(f"{BASE}/synopsis").json()
    assert row["content"] == [{"topic": "t", "gloss": "g"}]


def test_resume_keyless(client, kb):
    r = client.get(f"{BASE}/resume", params={"query": "anything"})
    assert r.status_code == 503 and r.json() == {"error": "embeddings unavailable"}
    r = client.get(f"{BASE}/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"] == "gap" and body["preamble"].startswith("<preamble>")


def test_explore_missing_keys_emits_sse_error(client, kb):
    r = client.post(f"{BASE}/explore", json={"prompt": "agent memory"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"phase": "error"' in r.text and "missing required keys" in r.text
