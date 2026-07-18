from __future__ import annotations

import importlib

import pytest


def _reload_cloud_server(monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "cloud")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    monkeypatch.setenv("CLOUD_SERVER_URL", "https://delapan-cloud.example")
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()

    import delapan.mcp.cloud_server as cs

    importlib.reload(cs)
    return cs


@pytest.mark.asyncio
async def test_registers_four_tools(monkeypatch):
    cs = _reload_cloud_server(monkeypatch)
    tools = await cs.mcp.list_tools()
    assert {t.name for t in tools} == {
        "delapan_resume", "delapan_search", "delapan_explore", "delapan_projects",
    }


def test_auth_points_at_supabase_issuer(monkeypatch):
    cs = _reload_cloud_server(monkeypatch)
    assert str(cs.mcp.settings.auth.issuer_url) == "https://x.supabase.co/auth/v1"
