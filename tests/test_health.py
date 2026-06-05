from __future__ import annotations

from fastapi.testclient import TestClient

from delapan.api.main import app


def test_health_ok():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["backend"] in ("local", "cloud")
