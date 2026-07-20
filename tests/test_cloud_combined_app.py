"""One Fly process serves MCP and the REST /api (spec §A/§C)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def combined(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_SERVER_URL", "https://test.invalid")
    monkeypatch.setenv("DELAPAN_BACKEND", "local")  # hermetic — app shape is under test
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("DLP_API__AUTH", "supabase")
    from delapan.core.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    from delapan.mcp.cloud_server import build_combined_app

    yield TestClient(build_combined_app())
    get_settings.cache_clear()
    get_config.cache_clear()


def test_health_served(combined):
    assert combined.get("/health").status_code == 200


def test_api_present_and_gated(combined):
    assert combined.get("/api/projects").status_code == 401  # mounted AND auth-gated
