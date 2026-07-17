"""Canvas surface: /canvas/search streams candidates+answer without persisting;
/canvas/keep persists through the resolver and returns its events."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from delapan.core.exploration.models import Finding


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp DB with no provider keys (embedding-free paths)."""
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "api.db"))
    for key in ("OPENAI_API_KEY", "TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
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


BASE = "/api/projects/proj/kbs/kb"


def store_db_path(_client) -> str:
    return os.environ["DELAPAN_DB_PATH"]


def _frames(sse_text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in sse_text.splitlines()
        if line.startswith("data: ")
    ]


def _candidate(title: str) -> Finding:
    now = datetime.now(timezone.utc)
    return Finding(
        exploration_id="e1",
        project_id="p1",
        category="cat",
        title=title,
        content={"k": "v"},
        confidence=0.4,
        provenance=[{"url": f"http://src/{title}"}],
        created_at=now,
        updated_at=now,
    )


def test_canvas_search_missing_keys_emits_sse_error(client, kb):
    r = client.post(f"{BASE}/canvas/search", json={"prompt": "agent memory"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    assert frames[-1]["phase"] == "error" and "missing required keys" in frames[-1]["error"]


def test_canvas_search_unknown_kb_is_404(client):
    assert client.post("/api/projects/nope/kbs/kb/canvas/search", json={"prompt": "x"}).status_code == 404


def test_canvas_search_streams_candidates_and_answer_without_persisting(
    client, kb, monkeypatch
):
    store, kb_id = kb
    for key in ("TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()

    from delapan.api import routes_canvas as canvas_mod

    async def _fake_run(prompt, *, exploration_id, project_id, kb_id, cfg, on_progress=None, **kw):
        if on_progress:
            await on_progress("planning")
            await on_progress("searching")
        return [_candidate("A"), _candidate("B")]

    async def _fake_preamble(query, *, store, kb_id, depth="shallow"):
        return "<preamble><empty/></preamble>", "gap"

    async def _fake_answer(prompt, *, preamble_xml, candidates, history, cfg):
        for chunk in ["Hello ", "world"]:
            yield chunk

    monkeypatch.setattr(canvas_mod, "run_exploration", _fake_run)
    monkeypatch.setattr(canvas_mod, "select_preamble", _fake_preamble)
    monkeypatch.setattr(canvas_mod, "stream_answer", _fake_answer)

    r = client.post(f"{BASE}/canvas/search", json={"prompt": "quota limits"})
    assert r.status_code == 200
    frames = _frames(r.text)
    phases = [f["phase"] for f in frames]

    assert phases[0] == "grounding" and frames[0]["coverage"] == "gap"
    assert "planning" in phases and "searching" in phases
    cand = next(f for f in frames if f["phase"] == "candidates")
    assert [c["title"] for c in cand["candidates"]] == ["A", "B"]
    assert cand["exploration_id"]
    assert [f["delta"] for f in frames if f["phase"] == "answer"] == ["Hello ", "world"]
    assert frames[-1] == {"phase": "completed", "candidate_count": 2}

    # Ephemeral: nothing persisted, but the run row exists and is completed-empty.
    assert store.count_findings(kb_id) == 0
    import sqlite3

    rows = sqlite3.connect(store_db_path(client)).execute(
        "select status, finding_ids from explorations"
    ).fetchall()
    assert rows[0][0] == "completed" and json.loads(rows[0][1]) == []


def test_canvas_search_provider_failure_emits_error_and_fails_row(client, kb, monkeypatch):
    for key in ("TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    from delapan.api import routes_canvas as canvas_mod
    from delapan.core.clients.tavily import TavilyError

    async def _fake_preamble(query, *, store, kb_id, depth="shallow"):
        return "<preamble><empty/></preamble>", "gap"

    async def _boom(prompt, **kwargs):
        raise TavilyError("search failed after 4 attempts: 432 quota")

    monkeypatch.setattr(canvas_mod, "select_preamble", _fake_preamble)
    monkeypatch.setattr(canvas_mod, "run_exploration", _boom)

    r = client.post(f"{BASE}/canvas/search", json={"prompt": "x"})
    frames = _frames(r.text)
    assert frames[-1]["phase"] == "error" and "432 quota" in frames[-1]["error"]
    import sqlite3

    rows = sqlite3.connect(store_db_path(client)).execute(
        "select status, error from explorations"
    ).fetchall()
    assert rows[0][0] == "failed" and "432 quota" in rows[0][1]


@pytest.fixture()
def keep_env(monkeypatch):
    """Keep path needs embeddings faked (no real keys) and memory enabled."""

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    from delapan.core.memory import persist as persist_mod

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    monkeypatch.setenv("DLP_MEMORY__ENABLED", "true")
    from delapan.core.config import get_config

    get_config.cache_clear()
    yield persist_mod
    get_config.cache_clear()


def _keep_payload(titles: list[str]) -> dict:
    return {
        "candidates": [
            {
                "category": "cat",
                "title": t,
                "content": {"k": f"fact about {t}"},
                "confidence": 0.4,
                "tags": [],
                "provenance": [{"url": f"http://src/{t}"}],
            }
            for t in titles
        ]
    }


def test_keep_persists_through_resolver_and_returns_events(client, kb, keep_env, monkeypatch):
    store, kb_id = kb
    persist_mod = keep_env

    from delapan.core.memory.models import ResolutionDecision, ResolutionOp

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)

    r = client.post(f"{BASE}/canvas/keep", json=_keep_payload(["A", "B"]))
    assert r.status_code == 200
    data = r.json()
    assert len(data["finding_ids"]) == 2
    assert [e["op"] for e in data["events"]] == ["ADD", "ADD"]
    assert all(e["new_finding_id"] for e in data["events"])
    assert "synopsis" in data
    assert store.count_findings(kb_id) == 2


def test_rekeep_noop_produces_no_duplicates(client, kb, keep_env, monkeypatch):
    store, kb_id = kb
    persist_mod = keep_env

    from delapan.core.memory.models import ResolutionDecision, ResolutionOp

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)
    first = client.post(f"{BASE}/canvas/keep", json=_keep_payload(["A"])).json()
    fid = first["finding_ids"][0]

    async def _noop(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP, target_finding_id=fid)
        ]

    monkeypatch.setattr(persist_mod, "resolve", _noop)
    second = client.post(f"{BASE}/canvas/keep", json=_keep_payload(["A"])).json()
    assert second["finding_ids"] == []
    assert [e["op"] for e in second["events"]] == ["NOOP"]
    assert store.count_findings(kb_id) == 1


def test_keep_clamps_count_and_content(client, kb, keep_env, monkeypatch):
    store, kb_id = kb
    persist_mod = keep_env
    seen = {}

    from delapan.core.memory.models import ResolutionDecision, ResolutionOp

    async def _spy_resolve(store_, kb_, cands, embs, mcfg):
        seen["cands"] = cands
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _spy_resolve)
    monkeypatch.setenv("DLP_CANVAS__KEEP_MAX_CANDIDATES", "2")
    monkeypatch.setenv("DLP_CANVAS__KEEP_MAX_CONTENT_CHARS", "10")
    from delapan.core.config import get_config

    get_config.cache_clear()

    payload = _keep_payload(["A", "B", "C"])
    payload["candidates"][0]["content"] = {"k": "x" * 50}
    r = client.post(f"{BASE}/canvas/keep", json=payload)
    assert r.status_code == 200
    assert len(seen["cands"]) == 2                       # count clamped
    assert seen["cands"][0].content == {"k": "x" * 10}   # content strings clamped
    get_config.cache_clear()


def test_keep_empty_candidates_is_400(client, kb):
    r = client.post(f"{BASE}/canvas/keep", json={"candidates": []})
    assert r.status_code == 400
