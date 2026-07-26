"""A zero-finding explore must say so — never a silent 'completed' with count 0."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "empty.db"))
    # fake keys: get past the Task-4 preflight without any network use
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()
    yield tmp_path / "empty.db"
    cfg.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_empty_run_reports_empty_status(env, monkeypatch):
    import delapan.mcp.server as s

    async def _no_findings(prompt, **kwargs):
        return []

    monkeypatch.setattr(s, "run_exploration", _no_findings)
    res = await s.delapan_explore("proj", "kb", prompt="anything")

    assert res["status"] == "empty"
    assert res["count"] == 0
    assert "reason" in res and "Tavily" in res["reason"]

    row = sqlite3.connect(env).execute(
        "SELECT status FROM explorations WHERE id = ?", (res["exploration_id"],)
    ).fetchone()
    assert row[0] == "empty"


@pytest.mark.asyncio
async def test_empty_promptless_run_returns_topic_to_backlog(env, monkeypatch):
    import delapan.mcp.server as s
    from delapan.core.config import CurationConfig
    from delapan.core.curation.recorder import _record
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("proj2", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "kb2", create=True)
    await _record(  # seed one gap topic — same idiom as test_curation_flywheel_e2e.py
        store,
        kb_id=kb_id,
        org_id=org_id,
        surface="resume",
        query="an unanswered demo question",
        coverage="gap",
        bands={1: [], 2: [], 3: []},
        embedding=[0.01] * 1536,
        cfg=CurationConfig(prune_sample_rate=0.0),
    )

    async def _no_findings(prompt, **kwargs):
        return []

    monkeypatch.setattr(s, "run_exploration", _no_findings)
    res = await s.delapan_explore("proj2", "kb2", prompt=None)
    assert res["status"] == "empty"

    # consumed topics leave the default backlog view; the empty run must have
    # un-consumed the topic, so it is visible again
    topics = await store.list_curation_topics(kb_id)
    assert topics, "empty run must return the consumed topic to the backlog"
