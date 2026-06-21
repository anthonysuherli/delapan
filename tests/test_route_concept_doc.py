from types import SimpleNamespace

from fastapi.testclient import TestClient

from delapan.api import routes_kg
from delapan.api.main import app

URL = "/api/projects/p/kbs/k/graph/nodes/n1/concept-doc"


def test_concept_doc_503_without_key(monkeypatch):
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key=None))
    res = TestClient(app).post(URL)
    assert res.status_code == 503
    assert res.json() == {"error": "llm unavailable"}


def test_concept_doc_200(monkeypatch):
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key="k"))
    monkeypatch.setattr(
        routes_kg, "resolve_kb_or_404",
        lambda project, kb: (SimpleNamespace(kb_id="kb", org_id="o"), object()),
    )

    async def fake_synth(store, kb_id, node_id):
        return {"description": "d", "body_markdown": "## b", "model": "m",
                "built_at": "2026-06-21T00:00:00Z", "grounded_hash": "f6fd8219"}

    monkeypatch.setattr(routes_kg, "synthesize_concept_doc", fake_synth)
    res = TestClient(app).post(URL)
    assert res.status_code == 200
    assert res.json()["grounded_hash"] == "f6fd8219"


def test_concept_doc_404(monkeypatch):
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key="k"))
    monkeypatch.setattr(
        routes_kg, "resolve_kb_or_404",
        lambda project, kb: (SimpleNamespace(kb_id="kb", org_id="o"), object()),
    )

    async def fake_synth(store, kb_id, node_id):
        raise LookupError(node_id)

    monkeypatch.setattr(routes_kg, "synthesize_concept_doc", fake_synth)
    res = TestClient(app).post(URL)
    assert res.status_code == 404
