from __future__ import annotations

from datetime import datetime, timezone

import pytest

from delapan.mcp import server as srv


class _Ctx:
    org_id, project_id, kb_id, access_token = "o", "p", "kb1", None


class _Store:
    def __init__(self, topics=None):
        self.topics = topics or []
        self.updates = []
        self.explorations = []

    async def match_findings(self, *_a, **_k):
        return [{"id": "f1", "title": "t", "content": "c", "similarity": 0.9}]

    async def list_curation_topics(self, _kb, *, include_closed=False, limit=None):
        return list(self.topics)

    async def update_curation_topic(self, _kb, topic_id, **patch):
        self.updates.append((topic_id, patch))

    def create_exploration(self, *_a, **_k):
        self.explorations.append("created")
        return "exp1"

    def update_exploration(self, *_a, **_k):
        pass


def _topic(tid="t1", *, text="what is csm", recurrence=3, coverage="gap"):
    return {
        "id": tid,
        "query_text": text,
        "query_norm": text,
        "coverage": coverage,
        "recurrence": recurrence,
        "first_seen": "2026-07-16T00:00:00+00:00",
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "consumed_at": None,
        "resolved_at": None,
    }


@pytest.fixture()
def patched(monkeypatch):
    store = _Store()
    monkeypatch.setattr(srv, "resolve_tenant", lambda *a, **k: _Ctx())
    monkeypatch.setattr(srv, "get_store", lambda *a, **k: store)
    monkeypatch.setattr(srv, "embed_text", _fake_embed)
    return store


async def _fake_embed(_q):
    return [0.1] * 1536


@pytest.mark.asyncio
async def test_search_records_verdict(patched, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "schedule_record", lambda *a, **k: calls.append(k))
    out = await srv.delapan_search(project="p", kb="kb", query="what is csm")
    assert out["findings"]  # results unchanged
    assert len(calls) == 1 and calls[0]["surface"] == "search"


@pytest.mark.asyncio
async def test_search_skips_recording_below_rich_hit_count(patched, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "schedule_record", lambda *a, **k: calls.append(k))
    await srv.delapan_search(project="p", kb="kb", query="what is csm", limit=2)
    assert calls == []  # a rich verdict is unreachable at limit < 3


@pytest.mark.asyncio
async def test_backlog_returns_ranked_topics(patched):
    patched.topics = [_topic("low", recurrence=1), _topic("high", text="q2", recurrence=9)]
    out = await srv.delapan_backlog(project="p", kb="kb")
    assert [t["id"] for t in out["topics"]] == ["high", "low"]


@pytest.mark.asyncio
async def test_promptless_explore_empty_backlog_creates_nothing(patched):
    patched.topics = []
    out = await srv.delapan_explore(project="p", kb="kb")
    assert "error" in out and "backlog empty" in out["error"]
    assert patched.explorations == []


@pytest.mark.asyncio
async def test_promptless_explore_consumes_top_topic(patched, monkeypatch):
    patched.topics = [_topic("t1", text="what is csm")]
    seen = {}

    async def _fake_run(prompt, **_k):
        seen["prompt"] = prompt
        return []

    monkeypatch.setattr(srv, "run_exploration", _fake_run)
    monkeypatch.setattr(srv, "resolve_and_persist", _fake_persist)
    monkeypatch.setattr(srv, "maybe_rebuild_synopsis", _fake_noop)
    monkeypatch.setattr(srv, "schedule_kg_update", lambda *a, **k: None)
    out = await srv.delapan_explore(project="p", kb="kb")
    assert seen["prompt"] == "what is csm"
    assert out["backlog_topic"] == "t1"
    assert any(u[0] == "t1" and u[1].get("consumed_at") for u in patched.updates)


@pytest.mark.asyncio
async def test_failed_promptless_explore_returns_topic_to_backlog(patched, monkeypatch):
    patched.topics = [_topic("t1")]

    async def _boom(*_a, **_k):
        raise RuntimeError("pipeline down")

    monkeypatch.setattr(srv, "run_exploration", _boom)
    with pytest.raises(RuntimeError):
        await srv.delapan_explore(project="p", kb="kb")
    assert any(u[0] == "t1" and u[1].get("consumed_at") is None for u in patched.updates)


class _Outcome:
    affected_finding_ids: list[str] = []


async def _fake_persist(*_a, **_k):
    return _Outcome()


async def _fake_noop(*_a, **_k):
    return None
