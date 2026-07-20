"""Provider failure must surface: engine re-raises, run row goes `failed`,
the explore SSE stream emits a terminal error frame. Pins the loud-failure
chain introduced with TavilyError (canvas v1 phase 1)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from delapan.core.clients import tavily as tavily_mod
from delapan.core.clients.tavily import TavilyError
from delapan.core.exploration import engine as engine_mod
from delapan.core.exploration.models import ExplorationPlan, SearchQuery


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp DB with no provider keys (embedding-free paths)."""
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "api.db"))
    for key in ("OPENAI_API_KEY", "TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "")  # force-empty, not delenv: the repo .env sits below process env
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


@pytest.fixture()
def quiet_narration(monkeypatch):
    monkeypatch.setenv("DLP_NARRATION__ENABLED", "false")
    from delapan.core.config import get_config

    get_config.cache_clear()
    yield
    get_config.cache_clear()


async def test_run_exploration_propagates_tavily_error(monkeypatch, quiet_narration):
    async def _fake_plan(prompt, cfg, lens="explore"):
        return ExplorationPlan(
            search_queries=[SearchQuery(query="q")],
            extraction_prompt="",
            expected_categories=[],
            finding_title_hint="",
        )

    async def _boom(query, *, max_results, search_depth):
        raise TavilyError("search failed after 4 attempts: 432 quota")

    monkeypatch.setattr(engine_mod, "plan_queries", _fake_plan)
    monkeypatch.setattr(tavily_mod, "search", _boom)

    phases: list[str] = []

    async def on_progress(phase: str) -> None:
        phases.append(phase)

    from delapan.core.config import get_config

    with pytest.raises(TavilyError):
        await engine_mod.run_exploration(
            "topic",
            exploration_id="e1",
            project_id="p1",
            kb_id="k1",
            cfg=get_config().exploration,
            on_progress=on_progress,
        )
    assert any(p.startswith("error") for p in phases)
    assert "completed" not in phases


def test_explore_route_marks_row_failed_and_emits_error_frame(
    tmp_path, monkeypatch, client, kb, quiet_narration
):
    # Keys must pass the guard so the pipeline (monkeypatched to fail) is reached.
    for key in ("TAVILY_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.setenv(key, "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()

    async def _boom(prompt, **kwargs):
        raise TavilyError("search failed after 4 attempts: 432 quota")

    from delapan.api import routes_explore as routes_mod

    monkeypatch.setattr(routes_mod, "run_exploration", _boom)

    r = client.post("/api/projects/proj/kbs/kb/explore", json={"prompt": "x"})
    assert r.status_code == 200
    assert '"phase": "error"' in r.text and "432 quota" in r.text

    rows = sqlite3.connect(str(tmp_path / "api.db")).execute(
        "select status, error from explorations"
    ).fetchall()
    assert len(rows) == 1
    status, error = rows[0]
    assert status == "failed" and "432 quota" in error
