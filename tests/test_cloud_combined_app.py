"""One Fly process serves MCP and the REST /api (spec §A/§C)."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def combined(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_SERVER_URL", "https://test.invalid")
    monkeypatch.setenv("DELAPAN_BACKEND", "local")  # hermetic — app shape is under test
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("DLP_API__AUTH", "supabase")
    # Dummy — not real network creds. Without these, cloud_server's module-level
    # `assert _settings.supabase_url` (and SupabaseTokenVerifier() construction)
    # silently fall through to whatever the repo-root `.env` supplies, which on
    # a dev machine is the PRODUCTION Supabase project — not hermetic.
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    from delapan.core.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    from delapan.mcp import cloud_server

    # cloud_server computes _settings/_cloud_server_url at import time — reload
    # so it picks up this test's env rather than whatever an earlier import saw.
    importlib.reload(cloud_server)

    yield TestClient(cloud_server.build_combined_app())
    get_settings.cache_clear()
    get_config.cache_clear()


def test_health_served(combined):
    assert combined.get("/health").status_code == 200


def test_api_present_and_gated(combined):
    assert combined.get("/api/projects").status_code == 401  # mounted AND auth-gated


def test_mcp_endpoint_unauthenticated_401_with_mcp_auth_semantics(combined):
    """/mcp must still route through the MCP app's OWN auth middleware, not
    silently fall through to (or be shadowed by) the REST app: the MCP-shaped
    WWW-Authenticate `resource_metadata` challenge only comes from
    ``RequireAuthMiddleware`` inside FastMCP's own streamable-http app. If
    ``build_combined_app`` ever regresses to routing /mcp into the REST app
    (or a bare 404), this header disappears and the assertion below catches
    it before the live claude.ai connector would."""
    r = combined.post("/mcp")
    assert r.status_code == 401
    www_auth = r.headers.get("www-authenticate", "")
    assert www_auth.startswith("Bearer ")
    assert "resource_metadata" in www_auth
