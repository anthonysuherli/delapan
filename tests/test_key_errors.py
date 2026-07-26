"""Missing credentials must produce one actionable message, never a traceback."""

from __future__ import annotations

import pytest


@pytest.fixture()
def keyless_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "keys.db"))
    for key in ("OPENAI_API_KEY", "TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "")  # force-empty: the repo .env sits below process env
    from delapan.core import config as cfg
    from delapan.core.clients import embeddings as emb

    cfg.get_settings.cache_clear()
    monkeypatch.setattr(emb, "_client", None)
    yield
    cfg.get_settings.cache_clear()


def test_missing_pipeline_keys_lists_both(keyless_env):
    from delapan.core.config import missing_pipeline_keys

    assert missing_pipeline_keys() == ["TAVILY_API_KEY", "AI_GATEWAY_API_KEY"]


def test_deps_reexport_still_works(keyless_env):
    from delapan.api.deps import missing_pipeline_keys

    assert "AI_GATEWAY_API_KEY" in missing_pipeline_keys()


def test_embed_client_raises_actionable(keyless_env):
    from delapan.core.clients.embeddings import MissingEmbeddingKeyError, _get_client

    with pytest.raises(MissingEmbeddingKeyError, match="AI_GATEWAY_API_KEY"):
        _get_client()


@pytest.mark.asyncio
async def test_search_returns_error_dict(keyless_env):
    import delapan.mcp.server as s
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("p", create=True)
    store.resolve_kb(org_id, project_id, "k", create=True)
    res = await s.delapan_search("p", "k", "anything")
    assert "AI_GATEWAY_API_KEY" in res["error"] and ".env" in res["error"]


@pytest.mark.asyncio
async def test_explore_preflight_blocks_before_run(keyless_env):
    import delapan.mcp.server as s

    res = await s.delapan_explore("p2", "k2", prompt="topic")
    assert "explore needs credentials" in res["error"]
    assert "TAVILY_API_KEY" in res["error"] and "AI_GATEWAY_API_KEY" in res["error"]


@pytest.mark.asyncio
async def test_explore_preflight_omits_env_advice_on_cloud_tier(keyless_env, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "cloud")
    import delapan.mcp.server as s
    from delapan.core.agent.state import TenantContext

    ctx = TenantContext(
        user_id="u",
        org_id="o",
        project_id="p",
        kb_id="k",
        thread_id="t",
        access_token="tok",
    )
    res = await s._explore_impl(ctx, "topic", None)
    assert ".env" not in res["error"]
    assert "TAVILY_API_KEY" in res["error"] and "AI_GATEWAY_API_KEY" in res["error"]
