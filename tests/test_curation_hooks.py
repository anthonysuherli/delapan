from __future__ import annotations

import pytest

from delapan.core.agent import preamble as pre


class _Store:
    async def match_findings(self, *_a, **_k):
        return [{"id": "f1", "title": "t", "content": "c", "category": "x",
                 "similarity": 0.9}]

    def load_synopsis(self, _kb):
        return None


@pytest.mark.asyncio
async def test_select_preamble_without_surface_does_not_record(monkeypatch):
    calls = []
    monkeypatch.setattr(pre, "schedule_record", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(pre, "embed_text", _fake_embed)
    await pre.select_preamble("what is csm", store=_Store(), kb_id="kb1")
    assert calls == []


@pytest.mark.asyncio
async def test_select_preamble_with_surface_records_verdict(monkeypatch):
    calls = []
    monkeypatch.setattr(pre, "schedule_record", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(pre, "embed_text", _fake_embed)
    _, coverage = await pre.select_preamble(
        "what is csm", store=_Store(), kb_id="kb1", surface="resume", org_id="o"
    )
    assert len(calls) == 1
    assert calls[0]["surface"] == "resume"
    assert calls[0]["coverage"] == coverage
    assert calls[0]["kb_id"] == "kb1" and calls[0]["org_id"] == "o"
    assert calls[0]["embedding"] is not None


@pytest.mark.asyncio
async def test_no_query_never_records(monkeypatch):
    calls = []
    monkeypatch.setattr(pre, "schedule_record", lambda *a, **k: calls.append(k))
    await pre.select_preamble(None, store=_Store(), kb_id="kb1", surface="resume", org_id="o")
    assert calls == []


async def _fake_embed(_q):
    return [0.1] * 1536
