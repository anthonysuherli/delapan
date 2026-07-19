# Curation Flywheel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every coverage verdict the engine computes, aggregate the gap/sparse ones into a recurrence-ranked backlog, and let a promptless `delapan_explore` consume the top topic — so the KB directs its own growth from real usage.

**Architecture:** Two layers behind the Store seam. `access_events` is append-only ground truth (sampled prune on a retention horizon); `curation_topics` is a write-time-materialized backlog where near-duplicate queries collapse into one row carrying a recurrence count. Recording is fire-and-forget from a background task and must never change hot-path latency or results. Topic assignment is exact-normalized-string first (enforced by a unique index), vector-match second, reusing the query embedding the hot path already computed — zero extra embed calls. All transition logic lives in one module (`recorder.py`); stores stay CRUD-dumb.

**Tech Stack:** Python 3.11+, pydantic v2 config, FastMCP (stdio), FastAPI (loopback), SQLite + sqlite-vec (local tier), Supabase/Postgres + pgvector (cloud tier), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-16-curation-flywheel-design.md` — read it before Task 1. This plan implements it; where they disagree, the spec wins.

## Global Constraints

- **Branch:** work on `docs/e3-e7-specs` in the worktree at `/private/tmp/claude-501/-Users-anthonysuherli-Repositories-8star-delapan-ai/03764e71-399b-4459-948c-f5beb0be8271/scratchpad/specs-wt`. **Do not touch `/Users/anthonysuherli/Projects/delapan` directly** — a concurrent session is implementing the write-path there on `feat/write-path-dedup`.
- **Never-raise contract:** every code path added under `_record` swallows all exceptions and logs at `logging.DEBUG`. Recording must never raise into a caller. (`store/base.py` `record_access` docstring is the contract.)
- **Byte-identical hot path:** `delapan_resume` / `delapan_search` results and latency must be identical with `curation.enabled` true or false. No new blocking call on the event loop: every new `SupabaseStore` method wraps its postgrest call in `asyncio.to_thread`.
- **Both store tiers, shape parity:** every new Store method exists on `SQLiteStore` and `SupabaseStore` and returns the same dict shape. The engine never imports a backend.
- **Every knob in config:** `CurationConfig` in `delapan/core/config.py` + a `curation:` block in `config.yaml`. Nothing hardcoded. Overridable as `DLP_CURATION__<FIELD>`.
- **All new Store methods are `async def`** — including on `SQLiteStore` (sqlite3 call inline, matching `match_findings`).
- **Increments happen in SQL** (`recurrence = recurrence + 1`), never read-modify-write in Python.
- **House style:** `from __future__ import annotations`, type hints throughout, terse module docstring with an ASCII flow diagram, ruff line-length 100.
- **Run tests with:** `cd <worktree> && uv run pytest <path> -v`
- **Cloud SQL is not applied by this plan.** Task 5 writes the migration to a file; applying it to the live instance is a manual gate (acceptance #11 in the spec).

---

### Task 1: CurationConfig

**Files:**
- Modify: `delapan/core/config.py` (add `CurationConfig` after `UserProfileConfig` ~line 228; register on `AppConfig` ~line 416)
- Modify: `config.yaml` (new `curation:` block)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CurationConfig` with fields `enabled: bool`, `record_search: bool`, `topic_match_threshold: float`, `recency_half_life_days: int`, `gap_weight: float`, `sparse_weight: float`, `backlog_limit: int`, `min_query_chars: int`, `events_retention_days: int`, `prune_sample_rate: float`; reachable as `get_config().curation`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_curation_defaults():
    from delapan.core.config import AppConfig

    c = AppConfig().curation
    assert c.enabled is True
    assert c.record_search is True
    assert c.topic_match_threshold == 0.83
    assert c.recency_half_life_days == 14
    assert c.gap_weight == 2.0
    assert c.sparse_weight == 1.0
    assert c.backlog_limit == 20
    assert c.min_query_chars == 8
    assert c.events_retention_days == 90
    assert c.prune_sample_rate == 0.01


def test_curation_env_override(monkeypatch):
    from delapan.core.config import get_config

    monkeypatch.setenv("DLP_CURATION__TOPIC_MATCH_THRESHOLD", "0.91")
    monkeypatch.setenv("DLP_CURATION__ENABLED", "false")
    get_config.cache_clear()
    try:
        cfg = get_config()
        assert cfg.curation.topic_match_threshold == 0.91
        assert cfg.curation.enabled is False
    finally:
        get_config.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_curation_defaults -v`
Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'curation'`

- [ ] **Step 3: Write minimal implementation**

In `delapan/core/config.py`, add after `UserProfileConfig` (before `class ExplorationConfig`):

```python
class CurationConfig(BaseModel):
    """Gap-driven curation flywheel: persist each coverage verdict, aggregate
    gap/sparse queries into a recurrence-ranked backlog, consume it explicitly.

    On by default — recording is zero-latency, best-effort and invisible; nothing
    changes until someone reads the backlog or omits `prompt` on explore. A
    dark-launched flywheel accumulates nothing and is useless when switched on.
    """

    enabled: bool = True  # kill-switch: no recording, no backlog, explore needs a prompt
    record_search: bool = True  # also record delapan_search verdicts
    topic_match_threshold: float = 0.83  # cosine sim for paraphrase→topic assignment
    recency_half_life_days: int = 14  # backlog score decay
    gap_weight: float = 2.0  # gap topics outrank sparse
    sparse_weight: float = 1.0
    backlog_limit: int = 20  # default view size
    min_query_chars: int = 8  # skip trivial queries
    events_retention_days: int = 90  # access_events prune horizon
    prune_sample_rate: float = 0.01  # P(a recording also prunes its KB)
```

In `class AppConfig`, add after the `memory` line:

```python
    curation: CurationConfig = Field(default_factory=CurationConfig)
```

In `config.yaml`, append:

```yaml
curation:
  enabled: true                # kill-switch: no recording, no backlog, explore requires a prompt
  record_search: true          # also record delapan_search verdicts
  topic_match_threshold: 0.83  # cosine sim for paraphrase→topic assignment (calibrate w/ E1)
  recency_half_life_days: 14   # backlog score decay
  gap_weight: 2.0              # gap topics outrank sparse
  sparse_weight: 1.0
  backlog_limit: 20            # default view size
  min_query_chars: 8           # skip trivial queries
  events_retention_days: 90    # access_events prune horizon
  prune_sample_rate: 0.01      # P(a recording also prunes its KB)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all tests, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add delapan/core/config.py config.yaml tests/test_config.py
git commit -m "feat(config): CurationConfig — flywheel knobs"
```

---

### Task 2: `rank_backlog` — pure ranking

**Files:**
- Create: `delapan/core/curation/__init__.py`
- Create: `delapan/core/curation/backlog.py`
- Test: `tests/test_curation_backlog.py`

**Interfaces:**
- Consumes: `CurationConfig` (Task 1).
- Produces: `rank_backlog(rows: list[dict], cfg: CurationConfig, now: datetime) -> list[dict]` — returns rows sorted by descending score, each annotated with a float `"score"` key. Input rows carry `recurrence: int`, `coverage: str`, `last_seen: str` (ISO-8601). Task 6 and Task 8 call this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_backlog.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from delapan.core.config import CurationConfig
from delapan.core.curation.backlog import rank_backlog

NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def _row(rid: str, *, recurrence: int, coverage: str, age_days: float) -> dict:
    return {
        "id": rid,
        "query_text": f"q-{rid}",
        "recurrence": recurrence,
        "coverage": coverage,
        "last_seen": (NOW - timedelta(days=age_days)).isoformat(),
    }


def test_empty_input():
    assert rank_backlog([], CurationConfig(), NOW) == []


def test_recurrence_orders():
    cfg = CurationConfig()
    rows = [
        _row("a", recurrence=1, coverage="gap", age_days=0),
        _row("b", recurrence=5, coverage="gap", age_days=0),
    ]
    assert [r["id"] for r in rank_backlog(rows, cfg, NOW)] == ["b", "a"]


def test_gap_outranks_sparse_at_equal_recurrence():
    cfg = CurationConfig()
    rows = [
        _row("sparse", recurrence=3, coverage="sparse", age_days=0),
        _row("gap", recurrence=3, coverage="gap", age_days=0),
    ]
    assert [r["id"] for r in rank_backlog(rows, cfg, NOW)] == ["gap", "sparse"]


def test_two_half_lives_scores_one_quarter():
    cfg = CurationConfig()  # recency_half_life_days = 14
    fresh = _row("fresh", recurrence=1, coverage="gap", age_days=0)
    old = _row("old", recurrence=1, coverage="gap", age_days=28)
    ranked = {r["id"]: r["score"] for r in rank_backlog([fresh, old], cfg, NOW)}
    assert ranked["old"] == pytest.approx(ranked["fresh"] * 0.25, rel=1e-6)


def test_unparseable_last_seen_does_not_raise():
    cfg = CurationConfig()
    rows = [{"id": "x", "recurrence": 2, "coverage": "gap", "last_seen": "not-a-date"}]
    out = rank_backlog(rows, cfg, NOW)
    assert len(out) == 1 and out[0]["score"] >= 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curation_backlog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan.core.curation'`

- [ ] **Step 3: Write minimal implementation**

Create `delapan/core/curation/__init__.py`:

```python
"""Gap-driven curation: record coverage verdicts → rank a backlog → consume it."""
```

Create `delapan/core/curation/backlog.py`:

```python
"""Backlog ranking — pure scoring over curation_topics rows.

    rows ─► score = recurrence × coverage_weight × exp(-age/half_life) ─► sorted

No IO: the store fetches, this ranks. Kept pure so the ranking is unit-testable
without a database and so both the MCP tool and the HTTP route share one rule.
"""

from __future__ import annotations

import math
from datetime import datetime

from delapan.core.config import CurationConfig


def _age_days(last_seen: str, now: datetime) -> float:
    """Age in days; unparseable/absent timestamps count as fresh (0.0)."""
    try:
        seen = datetime.fromisoformat(last_seen)
    except (TypeError, ValueError):
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - seen).total_seconds() / 86400.0)


def rank_backlog(rows: list[dict], cfg: CurationConfig, now: datetime) -> list[dict]:
    """Rank open topics by demand × severity × freshness, descending.

    Each returned row is the input dict plus a float ``score``. Rows are not
    mutated in place — callers get copies."""
    half_life = max(cfg.recency_half_life_days, 1)
    out: list[dict] = []
    for r in rows:
        weight = cfg.gap_weight if r.get("coverage") == "gap" else cfg.sparse_weight
        decay = math.exp(-_age_days(r.get("last_seen") or "", now) / half_life)
        score = float(r.get("recurrence") or 0) * weight * decay
        out.append({**r, "score": score})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_curation_backlog.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add delapan/core/curation/ tests/test_curation_backlog.py
git commit -m "feat(curation): rank_backlog — recurrence x severity x decay"
```

---

### Task 3: Store Protocol signatures

**Files:**
- Modify: `delapan/store/base.py` (extend `record_access`; add five methods in a new `# --- curation` section after the monitoring section)

**Interfaces:**
- Consumes: nothing.
- Produces: the Protocol contract every later task implements against —

```python
async def record_access(self, *, org_id, kb_id, surface, targets,
                        query_text=None, coverage=None, band_counts=None) -> None
async def match_curation_topics(self, kb_id, query_embedding, match_count, min_similarity) -> list[dict]
async def upsert_curation_topic(self, row: dict) -> str
async def bump_curation_topic(self, kb_id, topic_id, *, coverage, seen_at) -> None
async def update_curation_topic(self, kb_id, topic_id, **patch) -> None
async def list_curation_topics(self, kb_id, *, include_closed=False, limit=None) -> list[dict]
async def prune_access_events(self, kb_id, older_than_iso) -> None
```

Topic row shape (returned by `match_curation_topics` / `list_curation_topics`): `id, query_text, query_norm, coverage, recurrence, first_seen, last_seen, consumed_at, resolved_at` — plus `similarity` from `match_curation_topics` only. `upsert_curation_topic` takes `{org_id, kb_id, query_text, query_norm, embedding, coverage, seen_at}` and returns the topic id.

- [ ] **Step 1: Extend `record_access` and add the curation section**

There is no test step here — `base.py` is a `typing.Protocol` with `...` bodies, so it has no behavior to test. Tasks 4 and 5 test the implementations against it.

In `delapan/store/base.py`, replace the `record_access` signature with:

```python
    async def record_access(
        self,
        *,
        org_id: str,
        kb_id: str,
        surface: str,
        targets: list,
        query_text: str | None = None,
        coverage: str | None = None,
        band_counts: dict | None = None,
    ) -> None:
        """Append one access event. Best-effort by contract: **must never raise**.

        `coverage` is the rich/sparse/gap verdict for `query_text`, `band_counts`
        the per-band hit counts — the curation flywheel's ground truth. `targets`
        is accepted for Protocol compatibility and is not persisted by the
        query-level row; per-target fan-out rows can be added later without a
        schema change."""
        ...
```

Then append a new section after the monitoring section:

```python
    # --- curation flywheel ---------------------------------------------------
    # Backlog of gap/sparse queries, materialized at write time. All CRUD-dumb:
    # every transition rule lives in `core/curation/recorder.py`, once, not per
    # tier. All async — the cloud tier's postgrest calls go through to_thread so
    # a background recording never blocks the event loop.

    async def match_curation_topics(
        self,
        kb_id: str,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
    ) -> list[dict]:
        """Cosine KNN over this KB's topics; rows carry `similarity`.

        Row shape: `id, query_text, query_norm, coverage, recurrence, first_seen,
        last_seen, consumed_at, resolved_at, similarity`."""
        ...

    async def upsert_curation_topic(self, row: dict) -> str:
        """Insert a topic, or increment the existing one on `(kb_id, query_norm)`.

        Atomic: the conflict target is a unique index and the increment happens in
        SQL, so concurrent recordings of the same query can never double-insert or
        lose an update. On conflict: `recurrence += 1`, `last_seen`/`coverage`
        refreshed, `consumed_at`/`resolved_at` cleared. `row` carries `org_id,
        kb_id, query_text, query_norm, embedding, coverage, seen_at`. Returns the
        topic id."""
        ...

    async def bump_curation_topic(
        self, kb_id: str, topic_id: str, *, coverage: str, seen_at: str
    ) -> None:
        """Increment `recurrence` in SQL and refresh `last_seen`/`coverage`,
        clearing `consumed_at`/`resolved_at` — the vector-hit path's counterpart to
        `upsert_curation_topic`'s conflict arm."""
        ...

    async def update_curation_topic(self, kb_id: str, topic_id: str, **patch) -> None:
        """Patch stamp columns (`consumed_at`, `resolved_at`). Values are written
        verbatim; `None` clears."""
        ...

    async def list_curation_topics(
        self, kb_id: str, *, include_closed: bool = False, limit: int | None = None
    ) -> list[dict]:
        """Topics for `kb_id`. Open-only by default (`consumed_at IS NULL AND
        resolved_at IS NULL`); ranking is the caller's job (`rank_backlog`)."""
        ...

    async def prune_access_events(self, kb_id: str, older_than_iso: str) -> None:
        """Delete this KB's access events older than `older_than_iso`.
        Best-effort: never raises."""
        ...
```

- [ ] **Step 2: Verify the Protocol still imports and existing tests pass**

Run: `uv run pytest tests/test_store_sqlite.py tests/test_supabase_misc.py -v`
Expected: PASS — `record_access`'s new params are defaulted, so `tests/test_supabase_misc.py::test_record_access_never_raises` (which passes `targets=[]` and no verdict) is unaffected.

- [ ] **Step 3: Commit**

```bash
git add delapan/store/base.py
git commit -m "feat(store): curation Protocol — verdict recording + topic backlog"
```

---

### Task 4: SQLite tier — schema, `record_access`, five methods

**Files:**
- Modify: `delapan/store/sqlite.py` (`_SCHEMA` ~line 83; `record_access` ~line 1043; new curation methods after it)
- Test: `tests/test_store_sqlite.py`

**Interfaces:**
- Consumes: the Task 3 Protocol.
- Produces: working `SQLiteStore` implementations of all six methods. Task 6's `_record` calls them; Task 9's race test asserts against the real unique index.

**Note:** `access_events` does not exist in SQLite today, so both tables are brand-new `CREATE TABLE IF NOT EXISTS` in `_SCHEMA`. No `_ADD_COLUMN_MIGRATIONS` entry is needed on this tier — the ALTER is Supabase-only. (`_ensure_schema` runs `executescript(_SCHEMA)` *before* the migrations loop, so an index over a newly-ALTERed column would have to go in the loop; that gotcha does not bite here.)

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_store_sqlite.py`:

```python
from __future__ import annotations

import asyncio

import pytest

from delapan.store.sqlite import SQLiteStore

EMB = [0.1] * 1536
EMB_FAR = [-0.1] * 1536


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "t.db"))
    pid, _ = s.resolve_project("p", create=True)
    kid, _ = s.resolve_kb(pid, "kb", create=True)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curation_store_sqlite.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: access_events` (and `AttributeError` on the missing methods)

- [ ] **Step 3: Write minimal implementation**

In `delapan/store/sqlite.py`, append to `_SCHEMA` (after the `resolution_events` block):

```sql
CREATE TABLE IF NOT EXISTS access_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kb_id TEXT NOT NULL,
  target_type TEXT NOT NULL, target_id TEXT, surface TEXT NOT NULL, api_key_id TEXT,
  query_text TEXT, coverage TEXT, band_counts TEXT, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_access_events_kb_ts ON access_events(kb_id, ts);
CREATE TABLE IF NOT EXISTS curation_topics (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kb_id TEXT NOT NULL,
  query_text TEXT NOT NULL, query_norm TEXT NOT NULL, coverage TEXT NOT NULL,
  recurrence INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, consumed_at TEXT, resolved_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_curation_topics_norm ON curation_topics(kb_id, query_norm);
CREATE INDEX IF NOT EXISTS idx_curation_topics_kb ON curation_topics(kb_id);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_curation_topics USING vec0(topic_id TEXT, embedding float[1536]);
```

Replace `record_access` (the `return None` no-op) and append the curation methods:

```python
    # --- monitoring ----------------------------------------------------------

    async def record_access(
        self,
        *,
        org_id: str,
        kb_id: str,
        surface: str,
        targets: list,
        query_text: str | None = None,
        coverage: str | None = None,
        band_counts: dict | None = None,
    ) -> None:
        """Append one access event. Never raises (monitoring must not break callers).

        Access events are no longer only a cloud billing concern — they are the
        curation flywheel's ground truth, so the local tier persists them too.
        `targets` is accepted for Protocol parity and not written: this is the
        query-level row (`target_type='query'`), mirroring the cloud shape."""
        try:
            self._conn.execute(
                "INSERT INTO access_events (org_id, kb_id, target_type, target_id, "
                "surface, query_text, coverage, band_counts, ts) VALUES (?,?,?,?,?,?,?,?,?);",
                (
                    _ORG,
                    kb_id,
                    "query",
                    None,
                    surface,
                    query_text,
                    coverage,
                    json.dumps(band_counts) if band_counts is not None else None,
                    _now_iso(),
                ),
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001 — monitoring must never break the caller
            pass

    # --- curation flywheel ---------------------------------------------------

    async def match_curation_topics(
        self, kb_id: str, query_embedding: list[float], match_count: int, min_similarity: float
    ) -> list[dict]:
        q = serialize_float32(query_embedding)
        rows = self._conn.execute(
            f"""
            SELECT {_TOPIC_COLS}, vec_distance_cosine(v.embedding, ?) AS dist
            FROM vec_curation_topics v JOIN curation_topics t ON t.id = v.topic_id
            WHERE t.kb_id = ? ORDER BY dist LIMIT ?;
            """,
            (q, kb_id, match_count),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            similarity = 1.0 - float(r["dist"])
            if similarity < min_similarity:
                continue
            out.append({**_topic_from_row(r), "similarity": similarity})
        return out

    async def upsert_curation_topic(self, row: dict) -> str:
        """Insert-or-increment on (kb_id, query_norm). The unique index is the race
        fix — no lock, and the increment is done in SQL so it can't lose an update."""
        seen = row.get("seen_at") or _now_iso()
        new_id = uuid4().hex
        self._conn.execute(
            """
            INSERT INTO curation_topics (id, org_id, kb_id, query_text, query_norm,
                                         coverage, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(kb_id, query_norm) DO UPDATE SET
              recurrence = recurrence + 1, last_seen = excluded.last_seen,
              coverage = excluded.coverage, consumed_at = NULL, resolved_at = NULL;
            """,
            (
                new_id,
                _ORG,
                row["kb_id"],
                row["query_text"],
                row["query_norm"],
                row["coverage"],
                seen,
                seen,
            ),
        )
        tid = self._conn.execute(
            "SELECT id FROM curation_topics WHERE kb_id = ? AND query_norm = ?;",
            (row["kb_id"], row["query_norm"]),
        ).fetchone()["id"]
        if tid == new_id and row.get("embedding"):  # freshly inserted → index it
            self._conn.execute(
                "INSERT INTO vec_curation_topics (topic_id, embedding) VALUES (?, ?);",
                (tid, serialize_float32(row["embedding"])),
            )
        self._conn.commit()
        return tid

    async def bump_curation_topic(
        self, kb_id: str, topic_id: str, *, coverage: str, seen_at: str
    ) -> None:
        self._conn.execute(
            "UPDATE curation_topics SET recurrence = recurrence + 1, last_seen = ?, "
            "coverage = ?, consumed_at = NULL, resolved_at = NULL "
            "WHERE id = ? AND kb_id = ?;",
            (seen_at, coverage, topic_id, kb_id),
        )
        self._conn.commit()

    async def update_curation_topic(self, kb_id: str, topic_id: str, **patch) -> None:
        allowed = {"consumed_at", "resolved_at"}
        keys = [k for k in patch if k in allowed]
        if not keys:
            return
        sets = ", ".join(f"{k} = ?" for k in keys)
        self._conn.execute(
            f"UPDATE curation_topics SET {sets} WHERE id = ? AND kb_id = ?;",
            [*(patch[k] for k in keys), topic_id, kb_id],
        )
        self._conn.commit()

    async def list_curation_topics(
        self, kb_id: str, *, include_closed: bool = False, limit: int | None = None
    ) -> list[dict]:
        where = "t.kb_id = ?"
        if not include_closed:
            where += " AND t.consumed_at IS NULL AND t.resolved_at IS NULL"
        rows = self._conn.execute(
            f"SELECT {_TOPIC_COLS} FROM curation_topics t WHERE {where} "
            f"ORDER BY t.last_seen DESC LIMIT ?;",
            (kb_id, min(limit or 100, 500)),
        ).fetchall()
        return [_topic_from_row(r) for r in rows]

    async def prune_access_events(self, kb_id: str, older_than_iso: str) -> None:
        try:
            self._conn.execute(
                "DELETE FROM access_events WHERE kb_id = ? AND ts < ?;",
                (kb_id, older_than_iso),
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001 — best-effort
            pass
```

Add near the other module-level column constants (e.g. beside `_FINDING_MATCH_COLS`):

```python
_TOPIC_COLS = (
    "t.id, t.query_text, t.query_norm, t.coverage, t.recurrence, "
    "t.first_seen, t.last_seen, t.consumed_at, t.resolved_at"
)


def _topic_from_row(r) -> dict:
    """curation_topics row → the tier-parity dict shape."""
    return {
        "id": r["id"],
        "query_text": r["query_text"],
        "query_norm": r["query_norm"],
        "coverage": r["coverage"],
        "recurrence": r["recurrence"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "consumed_at": r["consumed_at"],
        "resolved_at": r["resolved_at"],
    }
```

Verify `json`, `uuid4`, and `serialize_float32` are already imported in `sqlite.py` (they are — used by `insert_findings`); add any that are missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_curation_store_sqlite.py tests/test_store_sqlite.py -v`
Expected: PASS (8 new tests + the existing SQLite suite green)

- [ ] **Step 5: Commit**

```bash
git add delapan/store/sqlite.py tests/test_curation_store_sqlite.py
git commit -m "feat(store/sqlite): access_events + curation_topics; record_access writes"
```

---

### Task 5: Cloud tier — migration file + `record_access` repair + five methods

**Files:**
- Create: `migrations/2026-07-16_curation_flywheel.sql` (create the `migrations/` dir if absent; check first — if the repo has no such dir, put it at `docs/superpowers/migrations/2026-07-16_curation_flywheel.sql` and say so in the commit message)
- Modify: `delapan/store/supabase.py` (`record_access` ~line 512; new curation methods after it)
- Test: `tests/test_curation_store_supabase.py`

**Interfaces:**
- Consumes: the Task 3 Protocol; `self._vec` (`supabase.py:35-37`) for embeddings; `self._org_id` for write scoping.
- Produces: `SupabaseStore` implementations with the same shapes as Task 4.

**The bug this task fixes:** today's cloud `record_access` inserts `targets` and `created_at` — **neither column exists** — and omits the `NOT NULL` `target_type`. `access_events` RLS is **SELECT-only**, so even a correct insert is rejected. Both failures are swallowed by the never-raise contract, which is why the dead code looked alive. The migration adds the INSERT/DELETE policies; this task fixes the shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_store_supabase.py`, following the fake-client style of `tests/test_supabase_misc.py` and `tests/test_supabase_findings.py` (read `tests/fake_supabase.py` first and use its `make_store` helper the same way those files do):

```python
from __future__ import annotations

import pytest

from tests.test_supabase_misc import make_store  # same fake-client harness

EMB = [0.1] * 1536


@pytest.mark.asyncio
async def test_record_access_matches_live_column_set(monkeypatch):
    """Regression test for the schema bug: the insert must carry target_type and
    must NOT carry targets/created_at (neither column exists on the live table)."""
    store, fake = make_store(monkeypatch)
    await store.record_access(
        org_id="ignored", kb_id="kb1", surface="resume", targets=["f1", "f2"],
        query_text="q", coverage="gap", band_counts={"1": 0},
    )
    payload = fake.table("access_events").last_insert
    assert set(payload) == {
        "org_id", "kb_id", "target_type", "target_id", "surface",
        "query_text", "coverage", "band_counts", "ts",
    }
    assert payload["target_type"] == "query"
    assert payload["org_id"] == store._org_id  # store self-scopes; arg ignored
    assert "targets" not in payload and "created_at" not in payload


@pytest.mark.asyncio
async def test_record_access_still_never_raises(monkeypatch):
    store, _ = make_store(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(store._c, "table", boom)
    await store.record_access(
        org_id="o", kb_id="kb1", surface="s", targets=[], coverage="gap"
    )


@pytest.mark.asyncio
async def test_upsert_calls_rpc_with_conventional_params(monkeypatch):
    store, fake = make_store(monkeypatch)
    await store.upsert_curation_topic({
        "org_id": "o", "kb_id": "kb1", "query_text": "what is csm",
        "query_norm": "what is csm", "embedding": EMB, "coverage": "gap",
        "seen_at": "2026-07-16T00:00:00+00:00",
    })
    name, params = fake.last_rpc
    assert name == "upsert_curation_topic"
    assert params["p_org_id"] == store._org_id
    assert params["p_embedding"].startswith("[") and params["p_embedding"].endswith("]")
    assert params["p_query_norm"] == "what is csm"


@pytest.mark.asyncio
async def test_match_calls_rpc_with_deployed_param_names(monkeypatch):
    store, fake = make_store(monkeypatch)
    await store.match_curation_topics("kb1", EMB, 5, 0.8)
    name, params = fake.last_rpc
    assert name == "match_curation_topics"
    assert set(params) == {"query_embedding", "match_kb_id", "match_count", "min_similarity"}
    assert params["match_kb_id"] == "kb1"
```

If `tests/fake_supabase.py` does not already expose `last_insert` / `last_rpc`, extend it minimally to record them — that is in scope for this task.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curation_store_supabase.py -v`
Expected: FAIL — `assert set(payload) == {...}` fails (today's payload has `targets`/`created_at`), and `AttributeError` on the missing methods.

- [ ] **Step 3: Write the migration file**

Create `migrations/2026-07-16_curation_flywheel.sql`:

```sql
-- E4 curation flywheel. Apply to the cloud instance before enabling on cloud.
-- Verified against the live schema: access_events is (id bigint, org_id uuid,
-- kb_id uuid, target_type text NOT NULL, target_id uuid, surface text NOT NULL,
-- api_key_id uuid, query_text text, ts timestamptz NOT NULL default now()).

-- 1. access_events: verdict columns (additive; rollup_access_events selects
--    explicit columns and is unaffected).
alter table access_events
  add column if not exists coverage text,
  add column if not exists band_counts jsonb;
create index if not exists idx_access_events_kb_ts on access_events (kb_id, ts);

-- 2. access_events RLS is SELECT-only today — writes and prunes are silently
--    rejected. Mirror the findings table's membership-based policies.
create policy access_events_insert on access_events for insert
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));
create policy access_events_delete on access_events for delete
  using (org_id in (select org_id from org_members where user_id = auth.uid()));

-- 3. curation_topics.
create table if not exists curation_topics (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  kb_id uuid not null references kbs(id),
  query_text text not null,
  query_norm text not null,
  embedding vector(1536),
  coverage text not null,
  recurrence int not null default 1,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  consumed_at timestamptz,
  resolved_at timestamptz);
create unique index if not exists uq_curation_topics_norm on curation_topics (kb_id, query_norm);
create index if not exists idx_curation_topics_open on curation_topics (kb_id)
  where consumed_at is null and resolved_at is null;

alter table curation_topics enable row level security;
create policy curation_topics_org on curation_topics for all
  using      (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

-- 4. RPCs. Param naming follows the deployed convention (match_findings uses
--    query_embedding/match_kb_id/match_count/min_similarity — not p_-prefixed).
create or replace function match_curation_topics(
  query_embedding vector(1536), match_kb_id uuid, match_count int, min_similarity real)
returns table (id uuid, query_text text, query_norm text, coverage text, recurrence int,
               first_seen timestamptz, last_seen timestamptz,
               consumed_at timestamptz, resolved_at timestamptz, similarity real)
language sql stable as $$
  select t.id, t.query_text, t.query_norm, t.coverage, t.recurrence, t.first_seen,
         t.last_seen, t.consumed_at, t.resolved_at,
         (1 - (t.embedding <=> query_embedding))::real
  from curation_topics t
  where t.kb_id = match_kb_id
    and t.embedding is not null
    and (1 - (t.embedding <=> query_embedding)) >= min_similarity
  order by t.embedding <=> query_embedding
  limit match_count;
$$;

-- Atomic insert-or-increment: PostgREST cannot express `recurrence = recurrence + 1`,
-- so the upsert must be an RPC. SECURITY INVOKER (default) => runs under the
-- caller's JWT => the policies above apply.
create or replace function upsert_curation_topic(
  p_org_id uuid, p_kb_id uuid, p_query_text text, p_query_norm text,
  p_embedding vector(1536), p_coverage text, p_seen timestamptz)
returns uuid language plpgsql as $$
declare v_id uuid;
begin
  insert into curation_topics (org_id, kb_id, query_text, query_norm, embedding,
                               coverage, first_seen, last_seen)
  values (p_org_id, p_kb_id, p_query_text, p_query_norm, p_embedding, p_coverage,
          p_seen, p_seen)
  on conflict (kb_id, query_norm) do update
    set recurrence  = curation_topics.recurrence + 1,
        last_seen   = excluded.last_seen,
        coverage    = excluded.coverage,
        consumed_at = null,
        resolved_at = null
  returning id into v_id;
  return v_id;
end; $$;

-- Atomic increment on the vector-hit path (kills the read-modify-write lost update).
create or replace function bump_curation_topic(
  p_topic_id uuid, p_coverage text, p_seen timestamptz)
returns void language sql as $$
  update curation_topics
     set recurrence = recurrence + 1, last_seen = p_seen, coverage = p_coverage,
         consumed_at = null, resolved_at = null
   where id = p_topic_id;
$$;
```

- [ ] **Step 4: Write the store implementation**

In `delapan/store/supabase.py`, replace `record_access` and append the curation methods:

```python
    # --- monitoring (best-effort, never raises) ------------------------------

    async def record_access(
        self,
        *,
        org_id: str,
        kb_id: str,
        surface: str,
        targets: list,
        query_text: str | None = None,
        coverage: str | None = None,
        band_counts: dict | None = None,
    ) -> None:
        """Append one query-level access event. Never raises.

        Shape matches the live table: `targets`/`created_at` do not exist there and
        `target_type` is NOT NULL — the old insert could never have landed. `targets`
        stays on the signature for Protocol parity and is not written; per-target
        fan-out rows can be added later with no schema change."""

        def _run() -> None:
            try:
                self._c.table("access_events").insert(
                    {
                        "org_id": self._org_id,
                        "kb_id": kb_id,
                        "target_type": "query",
                        "target_id": None,
                        "surface": surface,
                        "query_text": query_text,
                        "coverage": coverage,
                        "band_counts": band_counts,
                        "ts": _now_iso(),
                    }
                ).execute()
            except Exception:  # noqa: BLE001 — monitoring must never break the caller
                pass

        await asyncio.to_thread(_run)

    # --- curation flywheel ---------------------------------------------------

    async def match_curation_topics(
        self, kb_id: str, query_embedding: list[float], match_count: int, min_similarity: float
    ) -> list[dict]:
        params = {
            "query_embedding": self._vec(query_embedding),
            "match_kb_id": kb_id,
            "match_count": match_count,
            "min_similarity": min_similarity,
        }
        data = await asyncio.to_thread(
            lambda: self._c.rpc("match_curation_topics", params).execute().data
        )
        return data or []

    async def upsert_curation_topic(self, row: dict) -> str:
        params = {
            "p_org_id": self._org_id,
            "p_kb_id": row["kb_id"],
            "p_query_text": row["query_text"],
            "p_query_norm": row["query_norm"],
            "p_embedding": self._vec(row["embedding"]) if row.get("embedding") else None,
            "p_coverage": row["coverage"],
            "p_seen": row.get("seen_at") or _now_iso(),
        }
        data = await asyncio.to_thread(
            lambda: self._c.rpc("upsert_curation_topic", params).execute().data
        )
        return data if isinstance(data, str) else (data or [{}])[0].get("id", "")

    async def bump_curation_topic(
        self, kb_id: str, topic_id: str, *, coverage: str, seen_at: str
    ) -> None:
        params = {"p_topic_id": topic_id, "p_coverage": coverage, "p_seen": seen_at}
        await asyncio.to_thread(
            lambda: self._c.rpc("bump_curation_topic", params).execute()
        )

    async def update_curation_topic(self, kb_id: str, topic_id: str, **patch) -> None:
        allowed = {"consumed_at", "resolved_at"}
        body = {k: v for k, v in patch.items() if k in allowed}
        if not body:
            return
        await asyncio.to_thread(
            lambda: self._c.table("curation_topics").update(body)
            .eq("id", topic_id).eq("kb_id", kb_id).execute()
        )

    async def list_curation_topics(
        self, kb_id: str, *, include_closed: bool = False, limit: int | None = None
    ) -> list[dict]:
        def _run() -> list[dict]:
            q = self._c.table("curation_topics").select(
                "id, query_text, query_norm, coverage, recurrence, first_seen, "
                "last_seen, consumed_at, resolved_at"
            ).eq("kb_id", kb_id)
            if not include_closed:
                q = q.is_("consumed_at", "null").is_("resolved_at", "null")
            return q.order("last_seen", desc=True).limit(min(limit or 100, 500)).execute().data

        return await asyncio.to_thread(_run) or []

    async def prune_access_events(self, kb_id: str, older_than_iso: str) -> None:
        def _run() -> None:
            try:
                self._c.table("access_events").delete().eq("kb_id", kb_id).lt(
                    "ts", older_than_iso
                ).execute()
            except Exception:  # noqa: BLE001 — best-effort
                pass

        await asyncio.to_thread(_run)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_curation_store_supabase.py tests/test_supabase_misc.py -v`
Expected: PASS (4 new tests; `test_record_access_never_raises` still green)

- [ ] **Step 6: Commit**

```bash
git add migrations/ delapan/store/supabase.py tests/test_curation_store_supabase.py tests/fake_supabase.py
git commit -m "fix(store/supabase): repair record_access to the live schema; add curation methods

The old insert wrote targets/created_at (neither column exists) and omitted the
NOT NULL target_type, so it could never land; access_events RLS was SELECT-only,
so it would have been rejected anyway. Both were swallowed by the never-raise
contract. Migration adds the INSERT/DELETE policies and curation_topics."
```

---

### Task 6: The recorder — transitions + fire-and-forget

**Files:**
- Create: `delapan/core/curation/recorder.py`
- Test: `tests/test_curation_recorder.py`

**Interfaces:**
- Consumes: `CurationConfig` (Task 1); `rank_backlog` is *not* used here; the six Store methods (Tasks 3–5).
- Produces:
  - `normalize_query(q: str) -> str` — `" ".join(q.lower().split())`. Task 7 and Task 8 use it.
  - `schedule_record(store, *, kb_id, org_id, surface, query, coverage, bands, embedding) -> None` — fire-and-forget, returns immediately. Tasks 7 and 8 call it.
  - `async _record(...)` — the awaitable body; tests call it directly.

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_recorder.py`:

```python
from __future__ import annotations

import asyncio

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
        return self.matches

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curation_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan.core.curation.recorder'`

- [ ] **Step 3: Write minimal implementation**

Create `delapan/core/curation/recorder.py`:

```python
"""Verdict recording — the flywheel's write side, off the hot path.

    (query, verdict, bands, qvec) ─► record_access          (ground truth)
                                  ├─► gap/sparse ─► topic upsert | bump
                                  ├─► rich       ─► resolve matching topic
                                  └─► sampled prune

Every transition rule lives here, once — the stores stay CRUD-dumb, so the two
tiers cannot drift. Nothing in this module may raise into a caller: recording is
best-effort by contract (`Store.record_access`), and the hot path must be
byte-identical with curation on or off.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from delapan.core.config import CurationConfig, get_config
from delapan.store import Store

logger = logging.getLogger(__name__)

_BG_TASKS: set[asyncio.Task] = set()


def normalize_query(q: str) -> str:
    """Casefold + collapse whitespace — the exact-match key for `(kb_id, query_norm)`."""
    return " ".join((q or "").lower().split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def schedule_record(
    store: Store,
    *,
    kb_id: str,
    org_id: str,
    surface: str,
    query: str | None,
    coverage: str,
    bands: dict[int, list[dict]],
    embedding: list[float] | None,
) -> None:
    """Fire-and-forget verdict recording that won't be GC'd mid-flight.

    Holds a strong ref in a module-level set until the task finishes (CPython's
    event loop only weak-refs tasks, so an unreferenced create_task can vanish).
    Returns immediately: the caller never waits on the store."""
    cfg = get_config().curation
    if not cfg.enabled or not query:
        return
    try:
        task = asyncio.create_task(
            _record(
                store,
                kb_id=kb_id,
                org_id=org_id,
                surface=surface,
                query=query,
                coverage=coverage,
                bands=bands,
                embedding=embedding,
                cfg=cfg,
            )
        )
    except RuntimeError:  # no running loop (sync caller) — recording is optional
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _record(
    store: Store,
    *,
    kb_id: str,
    org_id: str,
    surface: str,
    query: str,
    coverage: str,
    bands: dict[int, list[dict]],
    embedding: list[float] | None,
    cfg: CurationConfig,
) -> None:
    """Persist one verdict and advance its topic. Never raises."""
    try:
        if not cfg.enabled or len((query or "").strip()) < cfg.min_query_chars:
            return

        band_counts = {str(b): len(rows) for b, rows in (bands or {}).items()}
        await store.record_access(
            org_id=org_id,
            kb_id=kb_id,
            surface=surface,
            targets=[],
            query_text=query,
            coverage=coverage,
            band_counts=band_counts,
        )

        if embedding:
            await _advance_topic(
                store,
                kb_id=kb_id,
                org_id=org_id,
                query=query,
                coverage=coverage,
                embedding=embedding,
                cfg=cfg,
            )

        if random.random() < cfg.prune_sample_rate:  # noqa: S311 — sampling, not crypto
            horizon = (_now() - timedelta(days=cfg.events_retention_days)).isoformat()
            await store.prune_access_events(kb_id, horizon)
    except Exception:  # noqa: BLE001 — recording must never break the caller
        logger.debug("curation: recording failed for kb=%s", kb_id, exc_info=True)


async def _advance_topic(
    store: Store,
    *,
    kb_id: str,
    org_id: str,
    query: str,
    coverage: str,
    embedding: list[float],
    cfg: CurationConfig,
) -> None:
    """Assign this query to a topic and apply the coverage transition.

    `rich` resolves a matching topic (the gap closed); `gap`/`sparse` bumps the
    match or inserts a new topic. The insert is an atomic upsert on
    `(kb_id, query_norm)`, so a concurrent identical recording increments rather
    than double-inserting."""
    now = _now().isoformat()
    hits = await store.match_curation_topics(kb_id, embedding, 1, cfg.topic_match_threshold)
    hit = hits[0] if hits else None

    if coverage == "rich":
        if hit:  # coverage flipped → the topic is done
            await store.update_curation_topic(kb_id, hit["id"], resolved_at=now)
        return

    if hit:
        await store.bump_curation_topic(kb_id, hit["id"], coverage=coverage, seen_at=now)
        return

    await store.upsert_curation_topic(
        {
            "org_id": org_id,
            "kb_id": kb_id,
            "query_text": query,
            "query_norm": normalize_query(query),
            "embedding": embedding,
            "coverage": coverage,
            "seen_at": now,
        }
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_curation_recorder.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add delapan/core/curation/recorder.py tests/test_curation_recorder.py
git commit -m "feat(curation): recorder — verdict persistence + topic transitions"
```

---

### Task 7: Hook the resume path (`select_preamble`, MCP tool, HTTP route)

**Files:**
- Modify: `delapan/core/agent/preamble.py` (`select_preamble` ~line 131)
- Modify: `delapan/mcp/server.py` (`delapan_resume` ~line 49)
- Modify: `delapan/api/routes_findings.py` (`resume` route ~line 72)
- Test: `tests/test_curation_hooks.py`

**Interfaces:**
- Consumes: `schedule_record` (Task 6).
- Produces: `select_preamble(query, *, store, kb_id, depth="normal", surface=None, org_id=None)` — unchanged return `tuple[str, Coverage]`. Recording fires only when `surface` is set. Task 8 relies on this signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_hooks.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curation_hooks.py -v`
Expected: FAIL with `AttributeError: module 'delapan.core.agent.preamble' has no attribute 'schedule_record'`

- [ ] **Step 3: Write minimal implementation**

In `delapan/core/agent/preamble.py`, add the import:

```python
from delapan.core.curation.recorder import schedule_record
```

Replace `select_preamble` with:

```python
async def select_preamble(
    query: str | None,
    *,
    store: Store,
    kb_id: str,
    depth: Depth = "normal",
    surface: str | None = None,
    org_id: str | None = None,
) -> tuple[str, Coverage]:
    """IO entry: load synopsis + (optional) band query matches → (xml, coverage).

    When `surface` is set (and a query exists), the verdict is recorded for the
    curation backlog — fire-and-forget, reusing the embedding computed here, so
    the returned preamble and this call's latency are unchanged. `surface=None`
    keeps every existing caller byte-identical."""
    cfg = get_config().tiers
    syn_row = load_synopsis(store, kb_id)
    synopsis = (syn_row or {}).get("content") or []

    bands: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    coverage: Coverage = "gap"
    if query:
        qvec = await embed_text(query)
        rows = await store.match_findings(
            kb_id,
            qvec,
            get_config().search.max_limit,
            # floor at the weakest band; band_findings drops anything below it
            cfg.band3_min,
        )
        bands = band_findings(rows or [], cfg)
        coverage = assess_coverage(bands, cfg)
        if surface:
            schedule_record(
                store,
                kb_id=kb_id,
                org_id=org_id or "",
                surface=surface,
                query=query,
                coverage=coverage,
                bands=bands,
                embedding=qvec,
            )

    return render_preamble(synopsis, bands, depth=depth, cfg=cfg), coverage
```

In `delapan/mcp/server.py`, in `delapan_resume`, change the `select_preamble` call to:

```python
    preamble, coverage = await select_preamble(
        query, store=store, kb_id=ctx.kb_id, depth=depth, surface="resume", org_id=ctx.org_id
    )
```

In `delapan/api/routes_findings.py`, in the `resume` route, change the call to:

```python
    preamble, coverage = await select_preamble(
        query or None, store=store, kb_id=ctx.kb_id, depth=depth,
        surface="resume", org_id=ctx.org_id,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_curation_hooks.py tests/test_mcp_smoke.py tests/test_api_routes.py -v`
Expected: PASS (3 new tests; the MCP smoke and API route suites still green)

- [ ] **Step 5: Commit**

```bash
git add delapan/core/agent/preamble.py delapan/mcp/server.py delapan/api/routes_findings.py tests/test_curation_hooks.py
git commit -m "feat(curation): record resume verdicts from select_preamble"
```

---

### Task 8: Hook search + add `delapan_backlog` + promptless explore + backlog route

**Files:**
- Modify: `delapan/mcp/server.py` (`delapan_search` ~line 70; `delapan_explore` ~line 88; new `delapan_backlog` tool; module docstring ~lines 6-12)
- Modify: `delapan/api/routes_findings.py` (new `GET /backlog`)
- Test: `tests/test_curation_surfaces.py`

**Interfaces:**
- Consumes: `band_findings` / `assess_coverage` (`preamble.py:38,53`), `schedule_record` (Task 6), `rank_backlog` (Task 2), `list_curation_topics` / `update_curation_topic` (Tasks 3–5).
- Produces: `delapan_backlog(project, kb, limit=None) -> {"topics": [...]}`; `delapan_explore(project, kb, prompt=None, max_findings=None)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_surfaces.py`:

```python
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
        "id": tid, "query_text": text, "query_norm": text, "coverage": coverage,
        "recurrence": recurrence, "first_seen": "2026-07-16T00:00:00+00:00",
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "consumed_at": None, "resolved_at": None,
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
    out = await srv.delapan_search.fn(project="p", kb="kb", query="what is csm")
    assert out["findings"]  # results unchanged
    assert len(calls) == 1 and calls[0]["surface"] == "search"


@pytest.mark.asyncio
async def test_search_skips_recording_below_rich_hit_count(patched, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "schedule_record", lambda *a, **k: calls.append(k))
    await srv.delapan_search.fn(project="p", kb="kb", query="what is csm", limit=2)
    assert calls == []  # a rich verdict is unreachable at limit < 3


@pytest.mark.asyncio
async def test_backlog_returns_ranked_topics(patched):
    patched.topics = [_topic("low", recurrence=1), _topic("high", text="q2", recurrence=9)]
    out = await srv.delapan_backlog.fn(project="p", kb="kb")
    assert [t["id"] for t in out["topics"]] == ["high", "low"]


@pytest.mark.asyncio
async def test_promptless_explore_empty_backlog_creates_nothing(patched):
    patched.topics = []
    out = await srv.delapan_explore.fn(project="p", kb="kb")
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
    out = await srv.delapan_explore.fn(project="p", kb="kb")
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
        await srv.delapan_explore.fn(project="p", kb="kb")
    assert any(u[0] == "t1" and u[1].get("consumed_at") is None for u in patched.updates)


class _Outcome:
    affected_finding_ids: list[str] = []


async def _fake_persist(*_a, **_k):
    return _Outcome()


async def _fake_noop(*_a, **_k):
    return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curation_surfaces.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'delapan_backlog'`, and `delapan_explore` requires `prompt`.

- [ ] **Step 3: Write minimal implementation**

In `delapan/mcp/server.py`, update the module docstring's tool list:

```python
A third entry path alongside the (cloud-only) HTTP API; it drains the same engine
through the Store seam, so one engine serves both tiers. The surface is
deliberately small — five tools:

    delapan_resume    — inject KB context (banner + preamble + coverage)
    delapan_search    — semantic search over existing findings
    delapan_explore   — run the research pipeline + persist findings
    delapan_backlog   — ranked gap/sparse queries awaiting research
    delapan_projects  — list the caller's projects/KBs
```

Add the imports:

```python
from datetime import datetime, timezone

from delapan.core.agent.preamble import assess_coverage, band_findings
from delapan.core.curation.backlog import rank_backlog
from delapan.core.curation.recorder import schedule_record
```

Replace `delapan_search`'s body tail (after `hits = await store.match_findings(...)`) with:

```python
    cur = get_config().curation
    tiers = get_config().tiers
    limit_used = limit or 10
    # A `rich` verdict needs `rich_hit_count` band-1 hits; below that the verdict
    # would be an artifact of the caller's limit, not of the KB.
    if cur.record_search and limit_used >= tiers.rich_hit_count:
        bands = band_findings(hits or [], tiers)
        schedule_record(
            store,
            kb_id=ctx.kb_id,
            org_id=ctx.org_id,
            surface="search",
            query=query,
            coverage=assess_coverage(bands, tiers),
            bands=bands,
            embedding=emb,
        )
    return {"query": query, "findings": hits}
```

Add the backlog tool after `delapan_search`:

```python
@mcp.tool()
async def delapan_backlog(project: str, kb: str, limit: int | None = None) -> dict:
    """The KB's curation backlog — gap/sparse queries it was asked and could not
    answer, ranked by recurrence × severity × recency. Returns ``{"topics": [...]}``,
    each with ``id, query_text, coverage, recurrence, last_seen, score``. Feed the
    top one to ``delapan_explore`` (or call explore with no prompt to consume it)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().curation
    rows = await store.list_curation_topics(ctx.kb_id, limit=limit or cfg.backlog_limit)
    ranked = rank_backlog(rows or [], cfg, datetime.now(timezone.utc))
    return {"topics": ranked[: (limit or cfg.backlog_limit)]}
```

Replace `delapan_explore`'s signature, docstring, and the head/except of its body:

```python
@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str | None = None, max_findings: int | None = None
) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating the project/KB on demand). Blocks until
    complete (may take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "finding_ids", "count"}``.

    With no ``prompt``, consumes the top item of the KB's curation backlog — the
    gap the KB was asked about most — and adds ``"backlog_topic"`` to the result.
    An empty backlog returns an error and creates nothing."""
    topic_id: str | None = None
    if prompt is None:
        try:
            ctx = resolve_tenant(project, kb, create=False)
        except Exception as exc:  # noqa: BLE001 — never create a KB to read a backlog
            return {"error": f"KB not found ({project}/{kb}): {exc}"}
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        cur = get_config().curation
        rows = await store.list_curation_topics(ctx.kb_id, limit=cur.backlog_limit)
        ranked = rank_backlog(rows or [], cur, datetime.now(timezone.utc))
        if not ranked:
            return {
                "error": "backlog empty — pass a prompt, or run resume/search so "
                "gaps get recorded"
            }
        top = ranked[0]
        topic_id, prompt = top["id"], top["query_text"]
        await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=_now_iso())
    else:
        ctx = resolve_tenant(project, kb, create=True)
        store = get_store(ctx.access_token, org_id=ctx.org_id)

    cfg = get_config().exploration
    cap = min(max_findings or cfg.default_max_findings, cfg.max_findings)

    exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, prompt)
    try:
        findings = await run_exploration(
            prompt,
            exploration_id=exp_id,
            project_id=ctx.project_id,
            kb_id=ctx.kb_id,
            cfg=cfg,
        )
        captured = findings[:cap]

        outcome = await resolve_and_persist(ctx, store, captured, get_config())
        ids = outcome.affected_finding_ids

        store.update_exploration(
            exp_id, status="completed", completed_at=_now_iso(), finding_ids=ids
        )
        # Grow the stable layers from the new findings. Synopsis rebuild is awaited
        # (best-effort, never raises); the KG update is fire-and-forget (sync
        # scheduler, gated on an approved intent schema — no-op otherwise).
        await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        if topic_id:  # a failed run must return the topic to the backlog
            try:
                await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=None)
            except Exception:  # noqa: BLE001 — best-effort; the raise below is the signal
                pass
        raise

    out = {"exploration_id": exp_id, "finding_ids": ids, "count": len(ids)}
    if topic_id:
        out["backlog_topic"] = topic_id
    return out
```

In `delapan/api/routes_findings.py`, add after the `resume` route:

```python
@router.get("/backlog")
async def backlog(project: str, kb: str, limit: int | None = None) -> JSONResponse:
    ctx, store = resolve_kb_or_404(project, kb)
    cfg = get_config().curation
    rows = await store.list_curation_topics(ctx.kb_id, limit=limit or cfg.backlog_limit)
    ranked = rank_backlog(rows or [], cfg, datetime.now(timezone.utc))
    return JSONResponse({"topics": ranked[: (limit or cfg.backlog_limit)]})
```

Add the imports it needs at the top of `routes_findings.py`:

```python
from datetime import datetime, timezone

from delapan.core.config import get_config
from delapan.core.curation.backlog import rank_backlog
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_curation_surfaces.py tests/test_mcp_smoke.py tests/test_api_routes.py -v`
Expected: PASS (6 new tests; existing suites green)

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/server.py delapan/api/routes_findings.py tests/test_curation_surfaces.py
git commit -m "feat(curation): backlog tool, search recording, promptless explore consume"
```

---

### Task 9: Flywheel end-to-end (the acceptance gate)

**Files:**
- Test: `tests/test_curation_flywheel_e2e.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8, against a real `SQLiteStore`.
- Produces: the acceptance proof. No production code — if this fails, fix the task that owns the defect.

This is spec acceptance criterion #10: a recorded gap becomes a backlog topic, a promptless explore consumes it, and a subsequent rich verdict on that same query resolves it off the backlog **without human input**.

- [ ] **Step 1: Write the test**

Create `tests/test_curation_flywheel_e2e.py`:

```python
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
    pid, _ = s.resolve_project("p", create=True)
    kid, _ = s.resolve_kb(pid, "kb", create=True)
    s._kb_id = kid
    return s


@pytest.mark.asyncio
async def test_gap_recurs_then_resolves_off_the_backlog(store):
    kb = store._kb_id

    # 1. The KB is asked the same thing three times and cannot answer.
    for _ in range(3):
        await _record(store, kb_id=kb, org_id="local", surface="resume", query=QUERY,
                      coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=CFG)

    topics = await store.list_curation_topics(kb)
    assert len(topics) == 1, "recurring gaps collapse into one topic"
    assert topics[0]["recurrence"] == 3
    top = topics[0]

    # 2. A promptless explore would consume it — stamp as the tool does.
    await store.update_curation_topic(kb, top["id"], consumed_at="2026-07-16T02:00:00+00:00")
    assert await store.list_curation_topics(kb) == [], "consumed topics leave the backlog"

    # 3. Explore filled the gap: the same query now reads rich.
    await _record(store, kb_id=kb, org_id="local", surface="resume", query=QUERY,
                  coverage="rich", bands=BANDS_RICH, embedding=EMB, cfg=CFG)

    closed = await store.list_curation_topics(kb, include_closed=True)
    assert len(closed) == 1 and closed[0]["resolved_at"] is not None
    assert await store.list_curation_topics(kb) == [], "resolved topic stays off the backlog"


@pytest.mark.asyncio
async def test_persisting_gap_after_consume_reopens_the_topic(store):
    kb = store._kb_id
    await _record(store, kb_id=kb, org_id="local", surface="resume", query=QUERY,
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=CFG)
    tid = (await store.list_curation_topics(kb))[0]["id"]
    await store.update_curation_topic(kb, tid, consumed_at="2026-07-16T02:00:00+00:00")

    # Explore ran but the gap persists — the topic must self-heal back on.
    await _record(store, kb_id=kb, org_id="local", surface="resume", query=QUERY,
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=CFG)

    open_topics = await store.list_curation_topics(kb)
    assert len(open_topics) == 1 and open_topics[0]["id"] == tid
    assert open_topics[0]["consumed_at"] is None
    assert open_topics[0]["recurrence"] == 2


@pytest.mark.asyncio
async def test_access_events_carry_the_verdict(store):
    kb = store._kb_id
    await _record(store, kb_id=kb, org_id="local", surface="resume", query=QUERY,
                  coverage="gap", bands=BANDS_GAP, embedding=EMB, cfg=CFG)
    row = store._conn.execute(
        "SELECT surface, coverage, query_text FROM access_events WHERE kb_id = ?;", (kb,)
    ).fetchone()
    assert row["surface"] == "resume" and row["coverage"] == "gap"
    assert row["query_text"] == QUERY
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_curation_flywheel_e2e.py -v`
Expected: PASS (3 tests). If any fail, the defect is in Tasks 4–8, not here.

- [ ] **Step 3: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS — no regressions anywhere.

- [ ] **Step 4: Commit**

```bash
git add tests/test_curation_flywheel_e2e.py
git commit -m "test(curation): flywheel end-to-end — gap recurs, consumes, resolves"
```

---

### Task 10: Docs + plugin surface

**Files:**
- Modify: `delapan/mcp/server.py` (module docstring — already done in Task 8; verify)
- Modify: `README.md` (tool list ~lines 24-26; "all 4 tools register and run" ~line 96)
- Modify: `/Users/anthonysuherli/Repositories/8star/delapan-ai/.claude-plugin/plugin.json`
- Modify: `/Users/anthonysuherli/Repositories/8star/delapan-ai/.claude-plugin/marketplace.json`
- Create: `/Users/anthonysuherli/Repositories/8star/delapan-ai/skills/backlog/SKILL.md`
- Modify: `/Users/anthonysuherli/Repositories/8star/delapan-ai/skills/explore/SKILL.md`

**Interfaces:**
- Consumes: `delapan_backlog` and promptless `delapan_explore` (Task 8).
- Produces: no code. The wrapper repo is a **separate git repo** — commit it separately (see Step 5).

**Note:** the four-tool surface is hardcoded in five places; `delapan_backlog` ships broken-in-docs without all of them. The sibling E5 spec also touches `server.py`'s docstring, the README tool list, the manifests, and `skills/explore/SKILL.md` — if E5 landed first, rebase rather than revert its edits.

**Drift, do not fix:** the workspace `CLAUDE.md` names `delapan-ai/docs/technical-overview.md` as authoritative and house rules say to update its "Current state" in the same change. **That file does not exist** in either repo. Do not create it to satisfy the rule; `README.md` is the real doc surface. Mention the stale pointer in the commit body.

- [ ] **Step 1: Verify the server docstring**

Run: `grep -n "five tools" delapan/mcp/server.py`
Expected: one hit (done in Task 8). If absent, apply the docstring change from Task 8 Step 3 now.

- [ ] **Step 2: Update the README**

In `README.md`, add to the tool list (after `delapan_explore`):

```markdown
| `delapan_backlog` | Ranked gap/sparse queries the KB was asked and couldn't answer |
```

Match the existing table's exact column shape — read lines 20-30 first and mirror them. Then change the line reading "all 4 tools register and run" to "all 5 tools register and run".

- [ ] **Step 3: Update the plugin manifests**

In both `/Users/anthonysuherli/Repositories/8star/delapan-ai/.claude-plugin/plugin.json` and `marketplace.json`, add a `backlog` entry to the skills list, mirroring the existing `search` entry's exact shape. Read each file first — do not guess the schema.

- [ ] **Step 4: Write the backlog skill**

Create `/Users/anthonysuherli/Repositories/8star/delapan-ai/skills/backlog/SKILL.md`, mirroring the frontmatter shape of `skills/search/SKILL.md` (read it first):

```markdown
---
name: backlog
description: Show the KB's curation backlog — gap/sparse queries it was asked and couldn't answer, ranked by demand. Use when deciding what to research next, or before running explore without a topic.
---

# Delapan Backlog

What the KB has been asked and failed to answer, ranked by recurrence × severity × recency.

## Target resolution

```bash
git rev-parse --show-toplevel | xargs basename   # → project
git branch --show-current                         # → kb
```

## Workflow

1. Call **`delapan_backlog`** with `project` and `kb`.
2. Present the ranked topics: `query_text`, `coverage`, `recurrence`, `score`.
3. To fill the top gap, call **`delapan_explore`** with **no `prompt`** — it consumes the top topic itself. To fill a different one, pass that topic's `query_text` as the prompt.

## Notes

- An empty backlog is normal for a new KB: topics only appear once `resume`/`search` records a gap or sparse verdict.
- A topic leaves the backlog when it is consumed by an explore, or when a later query on it reads `rich` (the gap closed).
- Recording is best-effort and never blocks: a KB with curation disabled always returns an empty backlog.
```

In `skills/explore/SKILL.md`, add to the Workflow section (its focus prompt is already documented as "optional" — this makes that true):

```markdown
- **No topic in mind?** Call `delapan_explore` with **no `prompt`** to consume the top item of the curation backlog (see the `backlog` skill). An empty backlog returns an error and creates nothing.
```

- [ ] **Step 5: Commit — two repos, two commits**

```bash
# engine repo (the worktree)
git add README.md delapan/mcp/server.py
git commit -m "docs: delapan_backlog — five tools, not four

The four-tool surface was hardcoded in the server docstring and README x2.
Note: CLAUDE.md points at delapan-ai/docs/technical-overview.md as the doc
surface to update; that file does not exist in either repo — README is the
real one. Stale pointer, not fixed here."

# wrapper repo (separate git repo)
cd /Users/anthonysuherli/Repositories/8star/delapan-ai
git add .claude-plugin/plugin.json .claude-plugin/marketplace.json skills/backlog/SKILL.md skills/explore/SKILL.md
git commit -m "feat(skills): backlog skill + promptless explore note"
```

---

## Verification checklist (run before calling this done)

- [ ] `uv run pytest -q` — whole suite green in the worktree.
- [ ] `uv run ruff check delapan/ tests/` — clean (line-length 100).
- [ ] Spec acceptance #8 (search parity): confirm `delapan_search`'s returned dict is identical with `curation.record_search` true vs false — covered by `test_search_records_verdict` + `test_search_skips_recording_below_rich_hit_count`, which assert on results, not just calls.
- [ ] Spec acceptance #11 (**live cloud gate — manual, deferred**): requires applying `migrations/2026-07-16_curation_flywheel.sql` to the cloud instance, then with `DELAPAN_BACKEND=cloud`, one `delapan_resume` must produce exactly one `access_events` row carrying `coverage`, readable back under the MCP user's JWT; `select rollup_access_events(current_date)` must still succeed and be idempotent across two calls. **Not done by this plan** — flag it to the user as the cloud enablement step.
- [ ] Spec acceptance #9 (E1 replay proof) is **out of scope**: E1's golden sets do not exist yet. Do not fake them.
