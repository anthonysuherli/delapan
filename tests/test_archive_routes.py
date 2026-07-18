from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "routes.db"))
    from delapan.api.main import app
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    s.resolve_kb("local", pid, "main", create=True)
    return TestClient(app)


def test_get_projects_returns_new_shape(client):
    body = client.get("/api/projects").json()
    kb = body["projects"][0]["kbs"][0]
    assert "finding_count" in kb
    assert "snapshot_count" not in kb


def test_patch_kb_archives_and_hides(client):
    r = client.patch("/api/projects/repoA/kbs/main", json={"archived": True})
    assert r.status_code == 200
    assert r.json()["archived_at"] is not None
    assert client.get("/api/projects").json()["projects"][0]["kbs"] == []


def test_patch_project_archives(client):
    r = client.patch("/api/projects/repoA", json={"archived": True})
    assert r.status_code == 200
    assert client.get("/api/projects").json()["projects"] == []
    assert client.get("/api/projects?include_archived=true").json()["projects"]


def test_patch_unknown_project_404s(client):
    r = client.patch("/api/projects/nope", json={"archived": True})
    assert r.status_code == 404
