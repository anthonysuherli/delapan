from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from delapan.api import routes_kg
from delapan.api.auth import request_tenancy
from delapan.api.main import app

URL = "/api/projects/p/kbs/k/graph/nodes/n1/concept-doc"

# concept_doc is now tenancy-gated via Depends(request_tenancy) (route dependency
# injection resolves before the function body), so tests override the dependency
# on the shared `app` singleton instead of monkeypatching the removed
# module-level `resolve_kb_or_404` reference. Cleared after every test so the
# override never leaks into other test modules sharing `app`.
_FAKE_TENANCY = (SimpleNamespace(kb_id="kb", org_id="o"), object())


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    yield
    app.dependency_overrides.pop(request_tenancy, None)


def test_concept_doc_503_without_key(monkeypatch):
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key=None))
    app.dependency_overrides[request_tenancy] = lambda: _FAKE_TENANCY
    res = TestClient(app).post(URL)
    assert res.status_code == 503
    assert res.json() == {"error": "llm unavailable"}


def test_concept_doc_200(monkeypatch):
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key="k"))
    app.dependency_overrides[request_tenancy] = lambda: _FAKE_TENANCY

    async def fake_synth(store, kb_id, node_id):
        return {"description": "d", "body_markdown": "## b", "model": "m",
                "built_at": "2026-06-21T00:00:00Z", "grounded_hash": "f6fd8219"}

    monkeypatch.setattr(routes_kg, "synthesize_concept_doc", fake_synth)
    res = TestClient(app).post(URL)
    assert res.status_code == 200
    assert res.json()["grounded_hash"] == "f6fd8219"


def test_concept_doc_404(monkeypatch):
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key="k"))
    app.dependency_overrides[request_tenancy] = lambda: _FAKE_TENANCY

    async def fake_synth(store, kb_id, node_id):
        raise LookupError(node_id)

    monkeypatch.setattr(routes_kg, "synthesize_concept_doc", fake_synth)
    res = TestClient(app).post(URL)
    assert res.status_code == 404


def test_concept_doc_503_on_gateway_error(monkeypatch):
    """A gateway/LLM failure (e.g. 402 insufficient_funds, 429, 5xx) must return
    a clean 503 — not bubble as an uncaught 500, whose missing CORS headers flip
    the SPA to offline mock mode."""
    monkeypatch.setattr(routes_kg, "get_settings", lambda: SimpleNamespace(ai_gateway_api_key="k"))
    app.dependency_overrides[request_tenancy] = lambda: _FAKE_TENANCY

    async def fake_synth(store, kb_id, node_id):
        raise RuntimeError("Error code: 402 - insufficient_funds")

    monkeypatch.setattr(routes_kg, "synthesize_concept_doc", fake_synth)
    res = TestClient(app).post(URL)
    assert res.status_code == 503
    assert res.json() == {"error": "llm unavailable"}
