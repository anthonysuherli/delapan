from __future__ import annotations

import pytest

from delapan.core.config import CurationConfig
from delapan.core.curation.recorder import _record
from delapan.store.sqlite import SQLiteStore

EMB = [0.1] * 1536
BANDS_GAP = {1: [], 2: [], 3: []}
BANDS_RICH = {1: [{"id": "a"}, {"id": "b"}, {"id": "c"}], 2: [], 3: []}
CFG = CurationConfig(prune_sample_rate=0.0)
QUERY = "what is the contractual service margin"


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "flywheel.db"))
    org_id, project_id = s.resolve_project("p", create=True)
    kb_id = s.resolve_kb(org_id, project_id, "kb", create=True)
    s._kb_id = kb_id
    return s


@pytest.mark.asyncio
async def test_gap_recurs_then_resolves_off_the_backlog(store):
    kb = store._kb_id

    # 1. The KB is asked the same thing three times and cannot answer.
    for _ in range(3):
        await _record(
            store,
            kb_id=kb,
            org_id="local",
            surface="resume",
            query=QUERY,
            coverage="gap",
            bands=BANDS_GAP,
            embedding=EMB,
            cfg=CFG,
        )

    topics = await store.list_curation_topics(kb)
    assert len(topics) == 1, "recurring gaps collapse into one topic"
    assert topics[0]["recurrence"] == 3
    top = topics[0]

    # 2. A promptless explore would consume it — stamp as the tool does.
    await store.update_curation_topic(
        kb, top["id"], consumed_at="2026-07-16T02:00:00+00:00"
    )
    assert await store.list_curation_topics(kb) == [], "consumed topics leave the backlog"

    # 3. Explore filled the gap: the same query now reads rich.
    await _record(
        store,
        kb_id=kb,
        org_id="local",
        surface="resume",
        query=QUERY,
        coverage="rich",
        bands=BANDS_RICH,
        embedding=EMB,
        cfg=CFG,
    )

    closed = await store.list_curation_topics(kb, include_closed=True)
    assert len(closed) == 1 and closed[0]["resolved_at"] is not None
    assert await store.list_curation_topics(kb) == [], "resolved topic stays off the backlog"


@pytest.mark.asyncio
async def test_persisting_gap_after_consume_reopens_the_topic(store):
    kb = store._kb_id
    await _record(
        store,
        kb_id=kb,
        org_id="local",
        surface="resume",
        query=QUERY,
        coverage="gap",
        bands=BANDS_GAP,
        embedding=EMB,
        cfg=CFG,
    )
    tid = (await store.list_curation_topics(kb))[0]["id"]
    await store.update_curation_topic(kb, tid, consumed_at="2026-07-16T02:00:00+00:00")

    # Explore ran but the gap persists — the topic must self-heal back on.
    await _record(
        store,
        kb_id=kb,
        org_id="local",
        surface="resume",
        query=QUERY,
        coverage="gap",
        bands=BANDS_GAP,
        embedding=EMB,
        cfg=CFG,
    )

    open_topics = await store.list_curation_topics(kb)
    assert len(open_topics) == 1 and open_topics[0]["id"] == tid
    assert open_topics[0]["consumed_at"] is None
    assert open_topics[0]["recurrence"] == 2


@pytest.mark.asyncio
async def test_access_events_carry_the_verdict(store):
    kb = store._kb_id
    await _record(
        store,
        kb_id=kb,
        org_id="local",
        surface="resume",
        query=QUERY,
        coverage="gap",
        bands=BANDS_GAP,
        embedding=EMB,
        cfg=CFG,
    )
    row = store._conn.execute(
        "SELECT surface, coverage, query_text FROM access_events WHERE kb_id = ?;", (kb,)
    ).fetchone()
    assert row["surface"] == "resume" and row["coverage"] == "gap"
    assert row["query_text"] == QUERY
