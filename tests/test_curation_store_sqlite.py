from __future__ import annotations

import asyncio

import pytest

from delapan.store.sqlite import SQLiteStore

EMB = [0.1] * 1536
EMB_FAR = [-0.1] * 1536


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "t.db"))
    org_id, pid = s.resolve_project("p", create=True)
    kid = s.resolve_kb(org_id, pid, "kb", create=True)
    s._kb_id = kid  # test convenience
    return s


def _kb(store) -> str:
    return store._kb_id


def _row(store, *, text="what is csm", norm="what is csm", coverage="gap") -> dict:
    return {
        "org_id": "local",
        "kb_id": _kb(store),
        "query_text": text,
        "query_norm": norm,
        "embedding": EMB,
        "coverage": coverage,
        "seen_at": "2026-07-16T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_record_access_writes_a_row(store):
    await store.record_access(
        org_id="local", kb_id=_kb(store), surface="resume", targets=[],
        query_text="q", coverage="gap", band_counts={"1": 0, "2": 1, "3": 2},
    )
    n = store._conn.execute(
        "SELECT COUNT(*) c FROM access_events WHERE kb_id = ?;", (_kb(store),)
    ).fetchone()["c"]
    assert n == 1


@pytest.mark.asyncio
async def test_upsert_inserts_then_increments(store):
    tid = await store.upsert_curation_topic(_row(store))
    again = await store.upsert_curation_topic(_row(store))
    assert again == tid  # same row, not a second
    rows = await store.list_curation_topics(_kb(store))
    assert len(rows) == 1 and rows[0]["recurrence"] == 2


@pytest.mark.asyncio
async def test_upsert_conflict_clears_stamps(store):
    tid = await store.upsert_curation_topic(_row(store))
    await store.update_curation_topic(_kb(store), tid, consumed_at="2026-07-16T01:00:00+00:00")
    assert await store.list_curation_topics(_kb(store)) == []  # closed → hidden
    await store.upsert_curation_topic(_row(store))
    rows = await store.list_curation_topics(_kb(store))
    assert len(rows) == 1 and rows[0]["consumed_at"] is None and rows[0]["recurrence"] == 2


@pytest.mark.asyncio
async def test_bump_increments_and_reopens(store):
    tid = await store.upsert_curation_topic(_row(store))
    await store.update_curation_topic(_kb(store), tid, resolved_at="2026-07-16T01:00:00+00:00")
    await store.bump_curation_topic(
        _kb(store), tid, coverage="sparse", seen_at="2026-07-16T02:00:00+00:00"
    )
    rows = await store.list_curation_topics(_kb(store))
    assert len(rows) == 1
    assert rows[0]["recurrence"] == 2
    assert rows[0]["coverage"] == "sparse"
    assert rows[0]["resolved_at"] is None


@pytest.mark.asyncio
async def test_match_finds_near_and_drops_far(store):
    await store.upsert_curation_topic(_row(store))
    hits = await store.match_curation_topics(_kb(store), EMB, 5, 0.5)
    assert len(hits) == 1 and hits[0]["similarity"] > 0.99
    assert await store.match_curation_topics(_kb(store), EMB_FAR, 5, 0.5) == []


@pytest.mark.asyncio
async def test_list_include_closed(store):
    tid = await store.upsert_curation_topic(_row(store))
    await store.update_curation_topic(_kb(store), tid, resolved_at="2026-07-16T01:00:00+00:00")
    assert await store.list_curation_topics(_kb(store)) == []
    assert len(await store.list_curation_topics(_kb(store), include_closed=True)) == 1


@pytest.mark.asyncio
async def test_prune_deletes_only_old(store):
    for ts in ("2026-01-01T00:00:00+00:00", "2026-07-16T00:00:00+00:00"):
        store._conn.execute(
            "INSERT INTO access_events (org_id, kb_id, target_type, surface, ts) "
            "VALUES (?,?,?,?,?);",
            ("local", _kb(store), "query", "resume", ts),
        )
    store._conn.commit()
    await store.prune_access_events(_kb(store), "2026-04-01T00:00:00+00:00")
    n = store._conn.execute(
        "SELECT COUNT(*) c FROM access_events WHERE kb_id = ?;", (_kb(store),)
    ).fetchone()["c"]
    assert n == 1


@pytest.mark.asyncio
async def test_concurrent_upsert_same_query_yields_one_row(store):
    await asyncio.gather(
        store.upsert_curation_topic(_row(store)),
        store.upsert_curation_topic(_row(store)),
    )
    rows = await store.list_curation_topics(_kb(store))
    assert len(rows) == 1 and rows[0]["recurrence"] == 2
