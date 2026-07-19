from __future__ import annotations

import pytest

from delapan.core.config import CurationConfig
from delapan.core.curation.recorder import _record, normalize_query

EMB = [0.1] * 1536
BANDS_GAP = {1: [], 2: [], 3: []}
BANDS_RICH = {1: [{"id": "a"}, {"id": "b"}, {"id": "c"}], 2: [], 3: []}


class StubStore:
    """Records calls; returns configurable match results."""

    def __init__(self, matches: list[dict] | None = None) -> None:
        self.matches = matches or []
        self.calls: list[tuple] = []

    async def record_access(self, **kw) -> None:
        self.calls.append(("record_access", kw))

    async def match_curation_topics(self, kb_id, emb, n, sim) -> list[dict]:
        self.calls.append(("match", kb_id, n, sim))
        return [m for m in self.matches if m.get("similarity", 0.0) >= sim]

    async def upsert_curation_topic(self, row) -> str:
        self.calls.append(("upsert", row))
        return "t-new"

    async def bump_curation_topic(self, kb_id, topic_id, *, coverage, seen_at) -> None:
        self.calls.append(("bump", topic_id, coverage))

    async def update_curation_topic(self, kb_id, topic_id, **patch) -> None:
        self.calls.append(("update", topic_id, patch))

    async def prune_access_events(self, kb_id, older_than_iso) -> None:
        self.calls.append(("prune", kb_id))


def _ops(store) -> list[str]:
    return [c[0] for c in store.calls]


@pytest.mark.asyncio
async def test_normalize_query():
    assert normalize_query("  What   IS  the CSM? ") == "what is the csm?"


@pytest.mark.asyncio
async def test_gap_with_no_match_inserts_topic():
    s = StubStore(matches=[])
    cfg = CurationConfig(prune_sample_rate=0.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=cfg)
    assert "record_access" in _ops(s) and "upsert" in _ops(s)
    row = [c for c in s.calls if c[0] == "upsert"][0][1]
    assert row["query_norm"] == "what is csm" and row["coverage"] == "gap"


@pytest.mark.asyncio
async def test_paraphrase_above_threshold_bumps_not_inserts():
    s = StubStore(matches=[{"id": "t1", "similarity": 0.9}])
    cfg = CurationConfig(topic_match_threshold=0.83, prune_sample_rate=0.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="define the csm",
                  coverage="sparse", bands=BANDS_GAP, embedding=EMB, cfg=cfg)
    assert "bump" in _ops(s) and "upsert" not in _ops(s)


@pytest.mark.asyncio
async def test_match_below_threshold_inserts():
    s = StubStore(matches=[{"id": "t1", "similarity": 0.5}])
    cfg = CurationConfig(topic_match_threshold=0.83, prune_sample_rate=0.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="unrelated thing",
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=cfg)
    assert "upsert" in _ops(s) and "bump" not in _ops(s)


@pytest.mark.asyncio
async def test_rich_stamps_resolved_on_matching_topic():
    s = StubStore(matches=[{"id": "t1", "similarity": 0.95}])
    cfg = CurationConfig(prune_sample_rate=0.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="rich", bands=BANDS_RICH, embedding=EMB, cfg=cfg)
    upd = [c for c in s.calls if c[0] == "update"]
    assert upd and upd[0][2]["resolved_at"] is not None
    assert "upsert" not in _ops(s) and "bump" not in _ops(s)


@pytest.mark.asyncio
async def test_rich_with_no_matching_topic_records_only():
    s = StubStore(matches=[])
    cfg = CurationConfig(prune_sample_rate=0.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="rich", bands=BANDS_RICH, embedding=EMB, cfg=cfg)
    assert _ops(s) == ["record_access", "match"]


@pytest.mark.asyncio
async def test_short_query_writes_nothing():
    s = StubStore()
    cfg = CurationConfig(min_query_chars=8)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="csm",
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=cfg)
    assert s.calls == []


@pytest.mark.asyncio
async def test_disabled_writes_nothing():
    s = StubStore()
    cfg = CurationConfig(enabled=False)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=cfg)
    assert s.calls == []


@pytest.mark.asyncio
async def test_never_raises_when_every_store_call_explodes():
    class Boom:
        def __getattr__(self, _name):
            async def _boom(*_a, **_k):
                raise RuntimeError("down")
            return _boom

    cfg = CurationConfig(prune_sample_rate=1.0)
    await _record(Boom(), kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=cfg)  # must not raise


@pytest.mark.asyncio
async def test_prune_fires_at_rate_one():
    s = StubStore(matches=[])
    cfg = CurationConfig(prune_sample_rate=1.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=cfg)
    assert "prune" in _ops(s)


@pytest.mark.asyncio
async def test_no_embedding_records_event_but_no_topic():
    s = StubStore(matches=[])
    cfg = CurationConfig(prune_sample_rate=0.0)
    await _record(s, kb_id="kb1", org_id="o", surface="resume", query="what is csm",
                  coverage="gap", bands=BANDS_GAP, embedding=None, cfg=cfg)
    assert _ops(s) == ["record_access"]
