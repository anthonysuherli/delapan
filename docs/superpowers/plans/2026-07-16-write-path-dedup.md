# Write-Path Integrity (E1+E2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop delapan's KB from duplicating itself — every persisted finding is resolved against existing knowledge (ADD/UPDATE/NOOP/SUPERSEDE) with bi-temporal supersede instead of deletion, backed by the engine's first tests for its coverage/merge heuristics.

**Architecture:** Adopt the `feat/mem0-resolution-port` branch (resolver + single `resolve_and_persist` path + events log) and extend it: findings gain `valid_from` / `invalidated_at` / `superseded_by`; retrieval reads live rows only; contradictions supersede rather than delete. A golden-query eval harness plus band recalibration make each step measurable. Cloud tier stays on pure-ADD behind a code guard until `SupabaseStore` reaches parity (the branch predates it).

**Tech Stack:** Python 3.11+, pydantic v2, pytest + pytest-asyncio (`asyncio_mode = "auto"`), SQLite + sqlite-vec (local tier), Supabase/Postgres + pgvector (cloud tier), Vercel AI Gateway for LLM/embeddings.

**Spec:** `docs/superpowers/specs/2026-07-16-write-path-dedup-design.md` — read it before Task 1. Section refs below (§C2, §C5…) point there.

## Global Constraints

- Repo root: `/Users/anthonysuherli/Projects/delapan` (a.k.a. `8star/delapan-ai/backend`). Branch: `master`. All paths below are relative to the repo root.
- House style: `from __future__ import annotations` at the top of every module; type hints throughout; terse module docstring with an ASCII flow diagram; ruff line-length 100.
- Run tests with `uv run pytest` (or `.venv/bin/pytest`). `asyncio_mode = "auto"` — async tests need no `@pytest.mark.asyncio`, though the existing suite includes it; match neighboring files.
- Run lint with `uv run ruff check delapan tests` before each commit.
- **A resolution failure must never drop a finding.** Every degradation path ends in ADD or an explicit skip+log.
- **Never delete a finding to dedup it.** Retirement is `invalidated_at` + `superseded_by` only.
- `_ORG` on the local tier is the literal `"local"`; ids are 32-char `uuid4().hex` strings (TEXT on SQLite, uuid on cloud).
- Do not touch: `core/knowledge_graph/`, `core/exploration/engine.py`, `core/agent/preamble.py` production logic (tests only), or anything listed under the spec's "Out of scope".
- Commit after every task with the message given in that task's final step.

---

### Task 1: Land the resolution branch dark + resolver sees neighbor bodies

Merges `feat/mem0-resolution-port` (verified conflict-free) with resolution **disabled**, so no behavior changes until Task 5. Also does §C1: the resolver's prompt still assumes neighbor bodies read back empty, which master fixed in `398c29a`.

**Files:**
- Merge: `feat/mem0-resolution-port` → `master`
- Modify: `config.yaml` (memory section), `delapan/core/config.py` (`MemoryConfig.enabled` default)
- Modify: `delapan/core/memory/resolver.py:1-16` (docstring), `:44-53` (`_candidate_block`), `:33-42` (`_SYSTEM`)
- Test: `tests/test_store_sqlite.py` (cherry-picked regression test)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `delapan.core.memory.persist.resolve_and_persist(ctx, store, candidates, cfg) -> ResolutionOutcome`; `delapan.core.memory.resolver.resolve(store, kb_id, candidates, embeddings, cfg) -> list[ResolutionDecision]`; `delapan.core.memory.models.{ResolutionOp, ResolutionDecision, ResolutionBatch, ResolutionEvent, ResolutionOutcome}`; `delapan.core.exploration.render.{render_content, normalize_provenance}`; `AppConfig.memory: MemoryConfig`; Store methods `update_finding`, `insert_resolution_events`, `list_resolution_events`.

- [ ] **Step 1: Verify the merge is clean before doing it**

```bash
cd /Users/anthonysuherli/Projects/delapan
git status --short          # expect a clean tree
git merge-tree --write-tree master feat/mem0-resolution-port >/dev/null && echo "CLEAN"
```

Expected: `CLEAN`. If it reports conflicts, stop and report — the spec assumed a clean merge.

- [ ] **Step 2: Merge the branch**

```bash
git merge --no-ff feat/mem0-resolution-port -m "merge: mem0-style finding resolution (dark — memory.enabled=false)"
```

Expected: merge commit created, no conflicts.

- [ ] **Step 3: Run the suite to confirm the merge is green**

Run: `uv run pytest -q`
Expected: all tests pass (the branch brings `tests/test_memory_*.py`, `tests/test_render.py`).

- [ ] **Step 4: Flip the kill-switch off in both places**

The branch ships resolution **on** in two independent places; both must be off so the merge is dark.

In `delapan/core/config.py`, `MemoryConfig`:

```python
    enabled: bool = False  # kill-switch: false → pure ADD, no resolver call
```

In `config.yaml`, the `memory:` section:

```yaml
memory:
  enabled: false  # flipped true at plan Task 8 (local tier only; cloud is guarded)
```

- [ ] **Step 5: Write a failing test that the kill-switch default is off**

Append to `tests/test_config.py`:

```python
def test_memory_resolution_disabled_by_default():
    from delapan.core.config import get_config

    get_config.cache_clear()
    assert get_config().memory.enabled is False
```

- [ ] **Step 6: Run it**

Run: `uv run pytest tests/test_config.py::test_memory_resolution_disabled_by_default -v`
Expected: PASS (Step 4 already made it true; this test locks the dark default until Task 8 flips it deliberately).

- [ ] **Step 7: Cherry-pick the content-roundtrip regression test only**

`fix/finding-content-roundtrip` is redundant (master fixed the bug in `398c29a`) and conflicts — take only its test:

```bash
git show fix/finding-content-roundtrip:tests/test_store_sqlite.py > /tmp/rt.py
```

Open `/tmp/rt.py`, find the test asserting a plain-string body reads back as the string (not `{}`), and append **only that test function** to `tests/test_store_sqlite.py`. Do not merge the branch.

- [ ] **Step 8: Run the regression test**

Run: `uv run pytest tests/test_store_sqlite.py -v`
Expected: PASS — master already round-trips string content.

- [ ] **Step 9: Update the resolver to use neighbor bodies**

The docstring's premise is now false. In `delapan/core/memory/resolver.py`, replace the stale paragraph in the module docstring:

```python
"""Memory resolver — decide ADD/UPDATE/NOOP/SUPERSEDE per candidate finding.

    candidates + neighbors ─► one LLM pass ─► [ResolutionDecision…]

Ported pattern from mem0's update-memory step (no mem0 dependency): for each new
candidate, retrieve the top-k semantically similar EXISTING findings and let the
model decide whether it is new (ADD), refines one (UPDATE), duplicates one (NOOP),
or contradicts one (SUPERSEDE). Tier-agnostic: uses only the Store contract.

Neighbor bodies round-trip correctly on both tiers, so the prompt shows the
model each neighbor's title AND body (same 600-char budget as candidates).
Any failure defaults to ADD — a resolver failure must never drop a finding.
"""
```

Then replace `_candidate_block` so neighbors carry bodies:

```python
def _candidate_block(i: int, f: Finding, neighbors: list[dict]) -> str:
    body = render_content(f.content)[:600]
    lines = [f"### candidate_index={i}", f"title: {f.title}", f"body: {body}", "neighbors:"]
    if not neighbors:
        lines.append("  (none)")
    for n in neighbors:
        sim = round(float(n.get("similarity", 0.0)), 3)
        lines.append(f"  - id={n.get('id')} sim={sim} title={n.get('title')!r}")
        n_body = render_content(n.get("content"))[:600]
        if n_body:
            lines.append(f"    body: {n_body}")
    return "\n".join(lines)
```

Note: `ResolutionOp.SUPERSEDE` does not exist yet — the docstring is forward-looking; the enum changes in Task 5. Leave `_SYSTEM` alone for now (Task 5 rewords it with the op remap).

- [ ] **Step 10: Write a test that neighbor bodies reach the prompt**

Create `tests/test_memory_resolver_prompt.py`:

```python
from __future__ import annotations

from delapan.core.exploration.models import Finding
from delapan.core.memory.resolver import _candidate_block


def test_candidate_block_includes_neighbor_bodies():
    f = Finding(
        exploration_id="e",
        project_id="p",
        category="fact",
        title="Tavily pricing",
        content={"plan": "free 1000/mo"},
    )
    neighbors = [{"id": "abc", "similarity": 0.91, "title": "Tavily", "content": "costs $0"}]
    block = _candidate_block(0, f, neighbors)
    assert "free 1000/mo" in block          # candidate body
    assert "id=abc" in block                # neighbor identity
    assert "costs $0" in block              # neighbor body — the C1 fix


def test_candidate_block_tolerates_empty_neighbor_content():
    f = Finding(exploration_id="e", project_id="p", category="fact", title="T", content={"a": 1})
    block = _candidate_block(0, f, [{"id": "x", "similarity": 0.5, "title": "N", "content": {}}])
    assert "id=x" in block
    assert "(none)" not in block
```

- [ ] **Step 11: Run the resolver prompt tests**

Run: `uv run pytest tests/test_memory_resolver_prompt.py -v`
Expected: both PASS.

- [ ] **Step 12: Lint and commit**

```bash
uv run ruff check delapan tests
uv run pytest -q
git add -A
git commit -m "feat(memory): land resolution dark; resolver prompt shows neighbor bodies"
```

---

### Task 2: Bi-temporal columns + live-only reads (SQLite + Protocol)

Adds the three §C3 columns and makes every read path exclude retired rows. No resolver behavior yet — this is pure schema + filtering.

**Files:**
- Modify: `delapan/store/sqlite.py` — `_SCHEMA` (findings CREATE TABLE), `_ADD_COLUMN_MIGRATIONS`, `_ensure_schema`, `insert_findings`, `match_findings`, `list_findings`, `count_findings`, `_finding_from_row`, `_FINDING_COLS`
- Modify: `delapan/store/base.py` — `list_findings` / `count_findings` docstrings + signature
- Test: `tests/test_store_temporal.py` (create)

**Interfaces:**
- Consumes: Task 1's merged tree.
- Produces: findings rows carry `valid_from`, `invalidated_at`, `superseded_by`; `Store.list_findings(kb_id, category=None, limit=None, include_invalidated=False)`; `Store.count_findings(kb_id)` counts live rows only; `match_findings` never returns a row with `invalidated_at IS NOT NULL`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_temporal.py`:

```python
from __future__ import annotations

import pytest


async def _seed(store, title="T"):
    org, pid = store.resolve_project("tmp", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ids = await store.insert_findings(
        [
            {
                "org_id": org,
                "kb_id": kb,
                "title": title,
                "content": "body",
                "category": "fact",
                "confidence": 0.5,
                "tags": [],
                "provenance": [{"url": "http://a"}],
                "embedding": [0.01] * 1536,
            }
        ]
    )
    return kb, ids[0]


@pytest.mark.asyncio
async def test_insert_stamps_valid_from(store):
    kb, fid = await _seed(store)
    row = store.get_finding(kb, fid)
    assert row["valid_from"]                       # stamped by the write path
    assert row["invalidated_at"] is None           # live
    assert row["superseded_by"] is None


@pytest.mark.asyncio
async def test_invalidated_rows_hidden_from_match_and_count(store):
    kb, fid = await _seed(store)
    assert store.count_findings(kb) == 1
    hits = await store.match_findings(kb, [0.01] * 1536, match_count=10, min_similarity=0.0)
    assert [h["id"] for h in hits] == [fid]

    store._conn.execute(
        "UPDATE findings SET invalidated_at = ? WHERE id = ?;", ("2026-07-16T00:00:00Z", fid)
    )
    store._conn.commit()

    assert store.count_findings(kb) == 0
    hits = await store.match_findings(kb, [0.01] * 1536, match_count=10, min_similarity=0.0)
    assert hits == []
    assert store.list_findings(kb)["count"] == 0
    assert store.list_findings(kb, include_invalidated=True)["count"] == 1
    assert store.get_finding(kb, fid)["invalidated_at"] == "2026-07-16T00:00:00Z"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_store_temporal.py -v`
Expected: FAIL — `KeyError: 'valid_from'` / unexpected keyword `include_invalidated`.

- [ ] **Step 3: Add the columns to the schema**

In `delapan/store/sqlite.py`, add the three columns to the findings CREATE TABLE inside `_SCHEMA` (for fresh DBs):

```sql
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL, title TEXT, content TEXT,
  category TEXT, confidence REAL, tags TEXT, provenance TEXT, created_at TEXT NOT NULL,
  valid_from TEXT, invalidated_at TEXT, superseded_by TEXT);
```

(Keep the existing column list exactly as it is and append the three — do not reorder.)

And append to `_ADD_COLUMN_MIGRATIONS` (for existing DBs):

```python
    # 0009: bi-temporal write path — when a fact became current, when it was retired,
    # and the row that replaced it. NULL invalidated_at = live.
    "ALTER TABLE findings ADD COLUMN valid_from TEXT;",
    "ALTER TABLE findings ADD COLUMN invalidated_at TEXT;",
    "ALTER TABLE findings ADD COLUMN superseded_by TEXT;",
```

- [ ] **Step 4: Backfill valid_from for pre-existing rows**

`ALTER TABLE ADD COLUMN` cannot carry `CURRENT_TIMESTAMP`, so the migration pass backfills. In `_ensure_schema`, after the `_ADD_COLUMN_MIGRATIONS` loop, add:

```python
        # valid_from has no column default (SQLite forbids non-constant ADD COLUMN
        # defaults) — seed pre-existing rows from created_at; new rows are stamped
        # by insert_findings. Idempotent: only NULLs are touched.
        try:
            self._conn.execute(
                "UPDATE findings SET valid_from = created_at WHERE valid_from IS NULL;"
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001 — table may not exist yet on a fresh DB
            pass
```

- [ ] **Step 5: Surface the columns on reads and stamp them on writes**

In `delapan/store/sqlite.py`:

Extend `_FINDING_COLS` with the three new columns (append `"valid_from", "invalidated_at", "superseded_by"`), then extend `_finding_from_row` to carry them:

```python
        "created_at": r["created_at"],
        "valid_from": r["valid_from"],
        "invalidated_at": r["invalidated_at"],
        "superseded_by": r["superseded_by"],
    }
```

In `insert_findings`, add `valid_from` to the INSERT column list and stamp it:

```python
                """
                INSERT INTO findings
                  (id, org_id, kb_id, title, content, category, confidence, tags, provenance,
                   created_at, valid_from)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
```

with the matching value appended after `created_at`:

```python
                    row.get("created_at") or _now_iso(),
                    row.get("valid_from") or _now_iso(),
```

- [ ] **Step 6: Filter live rows on every read path**

In `match_findings`, add to the `where` list built before the query (alongside the existing kb/category predicates):

```python
        where.append("f.invalidated_at IS NULL")
```

In `count_findings`:

```python
    def count_findings(self, kb_id: str) -> int:
        """Exact LIVE finding count for `kb_id` (uncapped, unlike list_findings).
        Retired rows (invalidated_at set) are excluded — this drives synopsis
        rebuild_delta, which must track live knowledge."""
        r = self._conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE kb_id = ? AND invalidated_at IS NULL;",
            (kb_id,),
        ).fetchone()
        return int(r["n"])
```

In `list_findings`, add the parameter and predicate (keep the existing category/limit logic):

```python
    def list_findings(
        self,
        kb_id: str,
        category: str | None = None,
        limit: int | None = None,
        include_invalidated: bool = False,
    ) -> dict:
```

and add `"invalidated_at IS NULL"` to its WHERE clauses unless `include_invalidated`.

- [ ] **Step 7: Mirror the contract on the Protocol**

In `delapan/store/base.py`:

```python
    def list_findings(
        self,
        kb_id: str,
        category: str | None = None,
        limit: int | None = None,
        include_invalidated: bool = False,
    ) -> dict:
        """Most-recent findings in `kb_id`. Returns {"count", "findings"}.
        Live rows only unless `include_invalidated` — retired rows stay
        reachable for history/audit, never for retrieval."""
        ...

    def count_findings(self, kb_id: str) -> int:
        """Exact number of LIVE findings in `kb_id` (uncapped, unlike list_findings)."""
        ...
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_store_temporal.py tests/test_store_sqlite.py -v`
Expected: all PASS.

- [ ] **Step 9: Run the full suite (regression check)**

Run: `uv run pytest -q`
Expected: all PASS. `_FINDING_COLS` feeds several queries — if anything fails, it is a column-list mismatch, not a test bug.

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check delapan tests
git add -A
git commit -m "feat(store): bi-temporal finding columns; reads and counts see live rows only"
```

---

### Task 3: Audit-log columns (`new_finding_id`, `details`)

§C2's events log must reconstruct what each op did. The branch's table has neither column, and DBs created in Task 1's dark window already exist.

**Files:**
- Modify: `delapan/core/memory/models.py` (`ResolutionEvent`)
- Modify: `delapan/store/sqlite.py` — `_SCHEMA` (resolution_events), `_ADD_COLUMN_MIGRATIONS`, `insert_resolution_events`, `list_resolution_events`
- Modify: `delapan/store/base.py` (`insert_resolution_events` docstring)
- Test: `tests/test_memory_store.py` (extend — from the branch)

**Interfaces:**
- Consumes: Task 2's migration mechanism.
- Produces: `ResolutionEvent(op, candidate_title, target_finding_id=None, new_finding_id=None, details=None, reason="")`; `list_resolution_events` rows carry `new_finding_id` and `details` (decoded dict or None).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memory_store.py`:

```python
@pytest.mark.asyncio
async def test_resolution_events_record_new_id_and_details(store):
    org, pid = store.resolve_project("evt", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_resolution_events(
        kb,
        [
            {
                "op": "NOOP",
                "candidate_title": "dup",
                "target_finding_id": "t1",
                "new_finding_id": None,
                "details": {"merged_urls": ["http://a"], "confidence_before": 0.4,
                            "confidence_after": 0.64},
                "reason": "same fact",
            }
        ],
    )
    rows = store.list_resolution_events(kb)
    assert rows[0]["op"] == "NOOP"
    assert rows[0]["details"]["merged_urls"] == ["http://a"]
    assert rows[0]["new_finding_id"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_memory_store.py::test_resolution_events_record_new_id_and_details -v`
Expected: FAIL — `KeyError: 'details'` (the write silently swallows the bad column; the read has no such key).

- [ ] **Step 3: Extend the model**

In `delapan/core/memory/models.py`:

```python
class ResolutionEvent(BaseModel):
    """A log row describing one applied decision (persisted to resolution_events)."""

    op: str
    candidate_title: str
    target_finding_id: str | None = None
    new_finding_id: str | None = None  # row created by ADD/UPDATE/SUPERSEDE
    details: dict | None = None  # op-specific effect (NOOP: merged urls, confidence delta)
    reason: str = ""
```

- [ ] **Step 4: Extend the SQLite table both ways**

In `_SCHEMA`, the resolution_events CREATE TABLE:

```sql
CREATE TABLE IF NOT EXISTS resolution_events (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL,
  op TEXT NOT NULL, candidate_title TEXT, target_finding_id TEXT,
  new_finding_id TEXT, details TEXT,
  reason TEXT, created_at TEXT NOT NULL);
```

And append to `_ADD_COLUMN_MIGRATIONS`:

```python
    # 0010: audit an op's effect, not just its verdict — the row it created and
    # (for NOOP) the urls merged plus the confidence delta.
    "ALTER TABLE resolution_events ADD COLUMN new_finding_id TEXT;",
    "ALTER TABLE resolution_events ADD COLUMN details TEXT;",
```

- [ ] **Step 5: Write and read the new columns**

In `insert_resolution_events`, extend the INSERT:

```python
                    """
                    INSERT INTO resolution_events
                      (id, org_id, kb_id, op, candidate_title, target_finding_id,
                       new_finding_id, details, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        uuid.uuid4().hex,
                        _ORG,
                        kb_id,
                        e.get("op"),
                        e.get("candidate_title"),
                        e.get("target_finding_id"),
                        e.get("new_finding_id"),
                        _json_dump_maybe(e.get("details")),
                        e.get("reason"),
                        _now_iso(),
                    ),
```

In `list_resolution_events`, add both columns to the SELECT and decode `details` in the returned dict:

```python
            "SELECT id, op, candidate_title, target_finding_id, new_finding_id, details, "
            "reason, created_at "
            "FROM resolution_events WHERE kb_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?;",
```

with `"details": _json_load(r["details"], None),` and `"new_finding_id": r["new_finding_id"],` in each mapped row.

- [ ] **Step 6: Update the Protocol docstring**

In `delapan/store/base.py`, `insert_resolution_events`:

```python
        """Append resolution decision rows. Best-effort by contract.

        Each row carries ``op, candidate_title, target_finding_id, new_finding_id,
        details, reason``; ``op`` is one of ADD/UPDATE/NOOP/SUPERSEDE. ``details``
        is an op-specific JSON blob. No-op on an empty list."""
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_memory_store.py -v`
Expected: all PASS.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check delapan tests
uv run pytest -q
git add -A
git commit -m "feat(memory): audit an op's effect — new_finding_id + details on resolution_events"
```

---

### Task 4: Write primitives — `update_finding` None→keep, `invalidate_finding`, `supersede_finding`

The three §C2 primitives every later task builds on. `SQLiteStore.update_finding` currently NULLs the body when `content=None`, which Task 5's NOOP path would trigger on every corroboration.

**Files:**
- Modify: `delapan/store/base.py` (Protocol: `update_finding`, + two new methods)
- Modify: `delapan/store/sqlite.py` (`update_finding`, + two new methods)
- Test: `tests/test_store_primitives.py` (create)

**Interfaces:**
- Consumes: Task 2's columns.
- Produces:
  - `update_finding(kb_id, finding_id, *, content=None, confidence=None, provenance=None, embedding=None, title=None) -> None` — every field optional; `None` means **keep**.
  - `invalidate_finding(kb_id, finding_id, *, superseded_by=None) -> None` — retire in place, no insert.
  - `supersede_finding(kb_id, target_id, new_row) -> str` — insert `new_row`, then retire `target_id` pointing at the new id, **in one transaction**; returns the new id.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_primitives.py`:

```python
from __future__ import annotations

import pytest


async def _seed(store, title="T", content="body", conf=0.4):
    org, pid = store.resolve_project("prim", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ids = await store.insert_findings(
        [
            {
                "org_id": org, "kb_id": kb, "title": title, "content": content,
                "category": "fact", "confidence": conf, "tags": [],
                "provenance": [{"url": "http://a"}], "embedding": [0.01] * 1536,
            }
        ]
    )
    return kb, ids[0]


@pytest.mark.asyncio
async def test_update_finding_none_keeps_content_and_title(store):
    kb, fid = await _seed(store, title="Original", content="keep me")
    await store.update_finding(
        kb, fid, confidence=0.64, provenance=[{"url": "http://a"}, {"url": "http://b"}]
    )
    row = store.get_finding(kb, fid)
    assert row["content"] == "keep me"      # not NULLed
    assert row["title"] == "Original"       # not NULLed
    assert row["confidence"] == 0.64
    assert len(row["provenance"]) == 2


@pytest.mark.asyncio
async def test_update_finding_sets_given_fields(store):
    kb, fid = await _seed(store)
    await store.update_finding(kb, fid, content="new body", title="New")
    row = store.get_finding(kb, fid)
    assert row["content"] == "new body"
    assert row["title"] == "New"
    assert row["confidence"] == 0.4         # untouched


@pytest.mark.asyncio
async def test_invalidate_finding_retires_in_place(store):
    kb, fid = await _seed(store)
    await store.invalidate_finding(kb, fid, superseded_by="other123")
    row = store.get_finding(kb, fid)        # history stays readable
    assert row["invalidated_at"]
    assert row["superseded_by"] == "other123"
    assert store.count_findings(kb) == 0    # gone from live
    assert await store.match_findings(kb, [0.01] * 1536, 10, 0.0) == []


@pytest.mark.asyncio
async def test_supersede_finding_inserts_then_retires_atomically(store):
    kb, old = await _seed(store, title="Old fact")
    new_row = {
        "kb_id": kb, "title": "New fact", "content": "corrected",
        "category": "fact", "confidence": 0.64, "tags": [],
        "provenance": [{"url": "http://b"}], "embedding": [0.02] * 1536,
    }
    new_id = await store.supersede_finding(kb, old, new_row)

    assert new_id != old
    assert store.count_findings(kb) == 1                      # net zero growth
    hits = await store.match_findings(kb, [0.02] * 1536, 10, 0.0)
    assert [h["id"] for h in hits] == [new_id]                # only the new row is live
    retired = store.get_finding(kb, old)
    assert retired["invalidated_at"]
    assert retired["superseded_by"] == new_id                 # forward pointer
    assert store.get_finding(kb, new_id)["valid_from"]        # stamped


@pytest.mark.asyncio
async def test_supersede_rolls_back_when_target_missing(store):
    kb, _ = await _seed(store)
    before = store.count_findings(kb)
    with pytest.raises(Exception):
        await store.supersede_finding(
            kb, "nonexistent", {"kb_id": kb, "title": "X", "content": "y",
                                "category": "fact", "confidence": 0.4, "tags": [],
                                "provenance": [], "embedding": [0.03] * 1536}
        )
    assert store.count_findings(kb) == before   # no orphaned insert survives
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_store_primitives.py -v`
Expected: FAIL — `AttributeError: 'SQLiteStore' object has no attribute 'invalidate_finding'`, and the None→keep test NULLs content.

- [ ] **Step 3: Make `update_finding` None→keep**

Replace `SQLiteStore.update_finding` in `delapan/store/sqlite.py`:

```python
    async def update_finding(
        self,
        kb_id: str,
        finding_id: str,
        *,
        content=None,
        confidence=None,
        provenance=None,
        embedding=None,
        title: str | None = None,
    ) -> None:
        """Partial in-place update; id stays stable (KG ``grounded_in`` refs hold).

        Every field is optional and ``None`` means KEEP the current value — the
        NOOP-corroborate path updates provenance/confidence only and must not
        clobber the body. Replaces the ``vec_findings`` row when an embedding is
        given."""
        sets: list[str] = []
        vals: list[object] = []
        if content is not None:
            sets.append("content = ?")
            vals.append(_json_dump_maybe(content))
        if confidence is not None:
            sets.append("confidence = ?")
            vals.append(confidence)
        if provenance is not None:
            sets.append("provenance = ?")
            vals.append(json.dumps(list(provenance)))
        if title is not None:
            sets.append("title = ?")
            vals.append(title)
        if sets:
            self._conn.execute(
                f"UPDATE findings SET {', '.join(sets)} WHERE id = ? AND kb_id = ?;",
                (*vals, finding_id, kb_id),
            )
        if embedding is not None:
            self._conn.execute("DELETE FROM vec_findings WHERE finding_id = ?;", (finding_id,))
            self._conn.execute(
                "INSERT INTO vec_findings (finding_id, embedding) VALUES (?, ?);",
                (finding_id, serialize_float32(list(embedding))),
            )
        self._conn.commit()
```

- [ ] **Step 4: Add `invalidate_finding` and `supersede_finding`**

Add both to `SQLiteStore`, directly after `update_finding`:

```python
    async def invalidate_finding(
        self, kb_id: str, finding_id: str, *, superseded_by: str | None = None
    ) -> None:
        """Retire a row in place — no insert. It leaves every read path
        (match/list/count) but stays readable via get_finding for history."""
        self._conn.execute(
            "UPDATE findings SET invalidated_at = ?, superseded_by = ? WHERE id = ? AND kb_id = ?;",
            (_now_iso(), superseded_by, finding_id, kb_id),
        )
        self._conn.commit()

    async def supersede_finding(self, kb_id: str, target_id: str, new_row: dict) -> str:
        """Insert ``new_row``, then retire ``target_id`` pointing at it. One
        transaction: a failure leaves the KB untouched (never a dangling
        invalidation, never an orphaned duplicate). Returns the new id."""
        new_id = new_row.get("id") or uuid.uuid4().hex
        now = _now_iso()
        try:
            self._conn.execute("BEGIN;")
            self._conn.execute(
                """
                INSERT INTO findings
                  (id, org_id, kb_id, title, content, category, confidence, tags, provenance,
                   created_at, valid_from)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    new_id,
                    _ORG,
                    kb_id,
                    new_row.get("title"),
                    _json_dump_maybe(new_row.get("content")),
                    new_row.get("category"),
                    new_row.get("confidence"),
                    json.dumps(list(new_row.get("tags") or [])),
                    json.dumps(list(new_row.get("provenance") or [])),
                    new_row.get("created_at") or now,
                    new_row.get("valid_from") or now,
                ),
            )
            embedding = new_row.get("embedding")
            if embedding is not None:
                self._conn.execute(
                    "INSERT INTO vec_findings (finding_id, embedding) VALUES (?, ?);",
                    (new_id, serialize_float32(list(embedding))),
                )
            cur = self._conn.execute(
                "UPDATE findings SET invalidated_at = ?, superseded_by = ? "
                "WHERE id = ? AND kb_id = ? AND invalidated_at IS NULL;",
                (now, new_id, target_id, kb_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"supersede target {target_id!r} not live in kb {kb_id!r}")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_id
```

- [ ] **Step 5: Add both to the Protocol**

In `delapan/store/base.py`, replace the `update_finding` stub with the partial-update contract and add the two new methods next to it:

```python
    async def update_finding(
        self,
        kb_id: str,
        finding_id: str,
        *,
        content=None,
        confidence=None,
        provenance=None,
        embedding=None,
        title: str | None = None,
    ) -> None:
        """Partial in-place update, keeping the id STABLE (so KG `grounded_in`
        references stay valid). Every field is optional; ``None`` means KEEP the
        current value. Re-indexes the embedding when one is given."""
        ...

    async def invalidate_finding(
        self, kb_id: str, finding_id: str, *, superseded_by: str | None = None
    ) -> None:
        """Retire a finding in place (no insert): stamp `invalidated_at` and an
        optional forward pointer. It disappears from match/list/count but stays
        readable via `get_finding`. Findings are never deleted to dedup them."""
        ...

    async def supersede_finding(self, kb_id: str, target_id: str, new_row: dict) -> str:
        """Insert `new_row` and retire `target_id` pointing at it, atomically.
        Returns the new finding id. Raises if the target is absent or already
        retired — leaving the KB unchanged."""
        ...
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_store_primitives.py -v`
Expected: all 5 PASS.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS. `tests/test_memory_persist.py` (from the branch) calls `update_finding` with all fields — still valid under the optional signature.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check delapan tests
git add -A
git commit -m "feat(store): partial update_finding, invalidate_finding, atomic supersede_finding"
```

---

### Task 5: Op remap — ADD/UPDATE/NOOP/SUPERSEDE + cloud guard

The heart of §C2. Replaces destructive DELETE and in-place UPDATE with corroboration and versioning, and keeps the cloud tier on pure ADD until Task 10.

**Files:**
- Modify: `delapan/core/memory/models.py` (`ResolutionOp`)
- Modify: `delapan/core/memory/resolver.py` (`_SYSTEM`, the op tuple in the target check)
- Modify: `delapan/core/memory/persist.py` (op application + cloud guard)
- Test: `tests/test_memory_persist.py` (extend the branch's file)

**Interfaces:**
- Consumes: Task 4's primitives; Task 3's `ResolutionEvent` fields.
- Produces: `ResolutionOp.SUPERSEDE` (replacing `DELETE`); `resolve_and_persist` applying the §C2 semantics; `ResolutionOutcome.affected_finding_ids` carrying **new** row ids for UPDATE/SUPERSEDE.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_memory_persist.py` (it already defines `_finding`; reuse it):

```python
async def _cfg_with_memory(monkeypatch):
    from delapan.core.config import get_config

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    monkeypatch.setattr(persist_mod, "active_backend", lambda: "local")
    get_config.cache_clear()
    cfg = get_config()
    cfg.memory.enabled = True
    return cfg


@pytest.mark.asyncio
async def test_noop_corroborates_target_and_raises_confidence(store, monkeypatch):
    org, pid = store.resolve_project("noopA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    f1 = _finding(pid, "Tavily pricing", {"k": "free 1000/mo"})
    f1.provenance = [{"url": "http://a"}]
    fid = (await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)).affected_finding_ids[0]
    before = store.get_finding(kb, fid)["confidence"]

    # A duplicate citing a NEW url → corroboration: same row, more sources.
    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP,
                               target_finding_id=fid, reason="same fact")
        ]),
    )
    f2 = _finding(pid, "Tavily pricing", {"k": "free 1000/mo"})
    f2.provenance = [{"url": "http://b"}]
    out = await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)

    assert out.affected_finding_ids == []          # NOOP touches no new row
    assert store.count_findings(kb) == 1           # no duplicate
    row = store.get_finding(kb, fid)
    assert {p["url"] for p in row["provenance"]} == {"http://a", "http://b"}
    assert row["confidence"] > before              # monotonic raise
    assert row["content"] == before_content_of(row)  # body untouched


@pytest.mark.asyncio
async def test_noop_with_no_new_url_is_a_true_no_write(store, monkeypatch):
    org, pid = store.resolve_project("noopB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    f1 = _finding(pid, "T", {"k": "v"})
    f1.provenance = [{"url": "http://a"}]
    fid = (await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)).affected_finding_ids[0]
    snapshot = store.get_finding(kb, fid)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP, target_finding_id=fid)
        ]),
    )
    f2 = _finding(pid, "T", {"k": "v"})
    f2.provenance = [{"url": "http://a"}]          # same url — nothing to corroborate
    await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)

    assert store.get_finding(kb, fid) == snapshot  # byte-identical row
    assert store.list_resolution_events(kb)[0]["op"] == "NOOP"  # but still audited


@pytest.mark.asyncio
async def test_supersede_retires_contradicted_row_without_deleting(store, monkeypatch):
    org, pid = store.resolve_project("supA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    old = _finding(pid, "Tavily is free", {"k": "free forever"})
    old.provenance = [{"url": "http://old"}]
    old_id = (await persist_mod.resolve_and_persist(ctx, store, [old], cfg)).affected_finding_ids[0]

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.SUPERSEDE,
                               target_finding_id=old_id, reason="contradicts")
        ]),
    )
    new = _finding(pid, "Tavily is paid", {"k": "$30/mo"})
    new.provenance = [{"url": "http://new"}]
    out = await persist_mod.resolve_and_persist(ctx, store, [new], cfg)

    new_id = out.affected_finding_ids[0]
    assert new_id != old_id
    assert store.count_findings(kb) == 1                     # old retired, new live
    retired = store.get_finding(kb, old_id)                  # NOT deleted
    assert retired["superseded_by"] == new_id
    # A contradicted finding's sources must not corroborate its contradiction.
    assert {p["url"] for p in store.get_finding(kb, new_id)["provenance"]} == {"http://new"}
    assert store.list_resolution_events(kb)[0]["op"] == "SUPERSEDE"


@pytest.mark.asyncio
async def test_update_versions_the_row_and_merges_provenance(store, monkeypatch):
    org, pid = store.resolve_project("updA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]),
    )
    f1 = _finding(pid, "Pricing", {"k": "old detail"})
    f1.provenance = [{"url": "http://a"}]
    old_id = (await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)).affected_finding_ids[0]

    monkeypatch.setattr(
        persist_mod, "resolve",
        lambda *a, **k: _decisions([
            ResolutionDecision(candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=old_id)
        ]),
    )
    f2 = _finding(pid, "Pricing", {"k": "refined detail"})
    f2.provenance = [{"url": "http://b"}]
    new_id = (await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)).affected_finding_ids[0]

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, old_id)["superseded_by"] == new_id
    new_row = store.get_finding(kb, new_id)
    assert {p["url"] for p in new_row["provenance"]} == {"http://a", "http://b"}  # union
    assert new_row["confidence"] > 0.4                                # two sources


@pytest.mark.asyncio
async def test_cloud_tier_forces_pure_add(store, monkeypatch):
    org, pid = store.resolve_project("guard", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    cfg = await _cfg_with_memory(monkeypatch)
    monkeypatch.setattr(persist_mod, "active_backend", lambda: "cloud")

    def _boom(*a, **k):
        raise AssertionError("resolver must not run on the cloud tier yet")

    monkeypatch.setattr(persist_mod, "resolve", _boom)
    await persist_mod.resolve_and_persist(ctx, store, [_finding(pid, "T", {"k": "v"})], cfg)
    assert store.count_findings(kb) == 1     # plain ADD, resolver never called
```

Add these two helpers at the top of the file (below the imports), used by the tests above:

```python
async def _decisions(ds):
    return ds


def before_content_of(row):
    """The body a NOOP must leave untouched (NOOP never rewrites content)."""
    return row["content"]
```

Note on `_decisions`: `monkeypatch.setattr(persist_mod, "resolve", lambda *a, **k: _decisions([...]))` returns the coroutine `resolve_and_persist` awaits.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_memory_persist.py -v`
Expected: FAIL — `AttributeError: SUPERSEDE` / `active_backend` not found in `persist_mod`.

- [ ] **Step 3: Remap the op enum**

In `delapan/core/memory/models.py`:

```python
class ResolutionOp(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"
    SUPERSEDE = "SUPERSEDE"
```

- [ ] **Step 4: Reword the resolver's prompt for the new vocabulary**

In `delapan/core/memory/resolver.py`, replace `_SYSTEM`:

```python
_SYSTEM = (
    "You maintain a knowledge base of findings. For each new CANDIDATE finding you "
    "are shown its most semantically-similar EXISTING findings (id, title, similarity, body). "
    "Decide ONE operation per candidate:\n"
    "- ADD: genuinely new information (no existing finding covers it).\n"
    "- UPDATE: refines/extends one existing finding (set target_finding_id to its id).\n"
    "- NOOP: duplicates one existing finding, no new info (set target_finding_id to its id).\n"
    "- SUPERSEDE: directly contradicts and invalidates one existing finding — the world "
    "changed or the old finding was wrong (set target_finding_id to its id). Use sparingly.\n"
    "Return one decision per candidate using the candidate_index you were given. "
    "When unsure, prefer ADD. Only UPDATE/NOOP/SUPERSEDE when a neighbor is clearly the same topic."
)
```

And in `resolve`, update the target-validation tuple:

```python
            if d.op in (ResolutionOp.UPDATE, ResolutionOp.NOOP, ResolutionOp.SUPERSEDE):
```

- [ ] **Step 5: Rewrite op application in `persist.py`**

In `delapan/core/memory/persist.py`, update the imports:

```python
from delapan.store import Store, active_backend
```

Add a helper above `resolve_and_persist`:

```python
def _distinct_urls(provenance: list[dict]) -> int:
    """Distinct source urls — url-less entries are kept but don't count as sources."""
    return len({p.get("url") for p in provenance if isinstance(p, dict) and p.get("url")})
```

Then replace the body of `resolve_and_persist` after the embed step. The kill-switch fast path gains the cloud guard:

```python
    # Kill-switch / cloud guard: pure ADD, no resolution, no events.
    # The cloud tier stays on pure ADD until SupabaseStore reaches parity with the
    # resolution write primitives (plan Task 10) — guard removed there.
    if not cfg.memory.enabled or active_backend() == "cloud":
        rows = [_row_from_candidate(ctx, f, emb) for f, emb in zip(candidates, embeddings)]
        ids = await store.insert_findings(rows)
        return ResolutionOutcome(affected_finding_ids=ids)
```

Replace the `NOOP`, `DELETE`, and `UPDATE` arms with:

```python
        elif d.op == ResolutionOp.NOOP:
            # Corroborate: the candidate's sources reinforce the existing finding.
            # No new url → nothing to corroborate → a true no-write (idempotent
            # re-runs leave the row byte-identical); the event is still logged.
            try:
                target = store.get_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — target vanished; skip and log
                events.append(
                    ResolutionEvent(
                        op="NOOP", candidate_title=f.title,
                        target_finding_id=d.target_finding_id,
                        reason="noop target missing; skipped",
                    )
                )
                continue
            existing_prov = target.get("provenance") or []
            merged_prov = _merge_provenance(existing_prov, normalize_provenance(f.provenance))
            before_n, after_n = _distinct_urls(existing_prov), _distinct_urls(merged_prov)
            before_conf = target.get("confidence") or 0.0
            after_conf = before_conf
            if after_n > before_n:
                # Monotonic: corroboration never lowers a confidence the extractor
                # or quality-blend set higher than the bare source curve.
                after_conf = max(before_conf, confidence_from_sources(after_n or 1))
                await store.update_finding(
                    ctx.kb_id,
                    d.target_finding_id,
                    confidence=after_conf,
                    provenance=merged_prov,
                )
            events.append(
                ResolutionEvent(
                    op="NOOP",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    details={
                        "merged_urls": sorted(
                            {p.get("url") for p in merged_prov if p.get("url")}
                        ),
                        "confidence_before": before_conf,
                        "confidence_after": after_conf,
                    },
                    reason=d.reason,
                )
            )
        elif d.op in (ResolutionOp.UPDATE, ResolutionOp.SUPERSEDE):
            try:
                target = store.get_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — target vanished → ADD instead
                add_rows.append(_row_from_candidate(ctx, f, emb))
                events.append(
                    ResolutionEvent(
                        op="ADD", candidate_title=f.title,
                        reason=f"{d.op.value.lower()} target missing; added",
                    )
                )
                continue
            row = _row_from_candidate(ctx, f, emb)
            if d.op == ResolutionOp.UPDATE:
                # Refinement inherits the target's sources — same claim, more support.
                row["provenance"] = _merge_provenance(
                    target.get("provenance") or [], normalize_provenance(f.provenance)
                )
            # SUPERSEDE keeps only its own provenance: a contradicted finding's
            # sources must not corroborate the claim that contradicts them.
            row["confidence"] = confidence_from_sources(_distinct_urls(row["provenance"]) or 1)
            try:
                new_id = await store.supersede_finding(ctx.kb_id, d.target_finding_id, row)
            except Exception:  # noqa: BLE001 — atomic: the KB is unchanged
                logger.warning(
                    "resolution %s failed for target %s; op not applied",
                    d.op.value, d.target_finding_id, exc_info=True,
                )
                continue
            affected.append(new_id)
            events.append(
                ResolutionEvent(
                    op=d.op.value,
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    new_finding_id=new_id,
                    reason=d.reason,
                )
            )
```

Also update the ADD arm so its event carries the new id — ADD ids are only known after the batched insert, so set them just before logging:

```python
    if add_rows:
        new_ids = await store.insert_findings(add_rows)
        affected.extend(new_ids)
        add_events = [e for e in events if e.op == "ADD"]
        for e, nid in zip(add_events, new_ids):
            e.new_finding_id = nid
```

And update the module docstring's op list to `ADD/UPDATE/NOOP/SUPERSEDE`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_memory_persist.py -v`
Expected: all PASS, including the branch's pre-existing tests.

- [ ] **Step 7: Check for stragglers referencing the old op**

Run: `grep -rn "ResolutionOp.DELETE\|\"DELETE\"" delapan/ tests/`
Expected: no hits in resolution code (`delete_finding` the API route is unrelated and stays).

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check delapan tests
git add -A
git commit -m "feat(memory): corroborate on NOOP, version on UPDATE, supersede instead of DELETE"
```

---

### Task 6: First tests for the coverage and merge heuristics

§C4a. These pure functions decide what every agent sees and have zero tests today. No production changes — if a test fails, report it rather than "fixing" the heuristic (Task 8 recalibrates deliberately).

**Files:**
- Test: `tests/test_preamble.py` (create), `tests/test_merger.py` (create)
- Read first: `delapan/core/agent/preamble.py`, `delapan/core/exploration/merger.py`, `delapan/core/config.py:192` (`TiersConfig`)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions).
- Produces: the regression net Task 8's recalibration is measured against.

- [ ] **Step 1: Write the preamble tests**

Create `tests/test_preamble.py`:

```python
from __future__ import annotations

from delapan.core.agent.preamble import assess_coverage, band_findings, render_preamble
from delapan.core.config import TiersConfig


def _rows(*sims):
    return [{"id": f"f{i}", "title": f"T{i}", "content": "body", "category": "fact",
             "similarity": s} for i, s in enumerate(sims)]


def test_bands_split_on_configured_thresholds():
    cfg = TiersConfig()
    bands = band_findings(_rows(0.9, cfg.band1_min, 0.5, cfg.band2_min, 0.3, cfg.band3_min), cfg)
    assert len(bands[1]) == 2      # >= band1_min
    assert len(bands[2]) == 2      # >= band2_min
    assert len(bands[3]) == 2      # >= band3_min


def test_rows_below_band3_are_dropped():
    cfg = TiersConfig()
    bands = band_findings(_rows(cfg.band3_min - 0.01, 0.0), cfg)
    assert bands == {1: [], 2: [], 3: []}


def test_bands_stay_similarity_desc():
    cfg = TiersConfig()
    bands = band_findings(_rows(0.6, 0.95, 0.7), cfg)
    sims = [r["similarity"] for r in bands[1]]
    assert sims == sorted(sims, reverse=True)


def test_coverage_rich_needs_enough_band1_hits():
    cfg = TiersConfig()
    n = cfg.rich_hit_count
    assert assess_coverage(band_findings(_rows(*([0.9] * n)), cfg), cfg) == "rich"
    assert assess_coverage(band_findings(_rows(*([0.9] * (n - 1))), cfg), cfg) == "sparse"


def test_coverage_gap_when_nothing_bands():
    cfg = TiersConfig()
    assert assess_coverage(band_findings(_rows(0.01), cfg), cfg) == "gap"
    assert assess_coverage(band_findings([], cfg), cfg) == "gap"


def test_preamble_respects_char_budget_dropping_weakest_first():
    cfg = TiersConfig(preamble_char_budget=900)
    rows = [
        {"id": "keep", "title": "Strong", "content": "x" * 400, "category": "fact",
         "similarity": 0.95},
        {"id": "drop", "title": "Weak", "content": "y" * 400, "category": "fact",
         "similarity": 0.56},
    ]
    out = render_preamble(band_findings(rows, cfg), synopsis=None, cfg=cfg, depth="normal")
    assert len(out) <= cfg.preamble_char_budget
    assert "Strong" in out and "Weak" not in out


def test_preamble_escapes_xml_in_titles():
    cfg = TiersConfig()
    rows = [{"id": "a", "title": 'A & B <script>', "content": "body", "category": "fact",
             "similarity": 0.9}]
    out = render_preamble(band_findings(rows, cfg), synopsis=None, cfg=cfg, depth="normal")
    assert "<script>" not in out
    assert "&amp;" in out or "&#" in out
```

Before running: open `delapan/core/agent/preamble.py` and check `render_preamble`'s real signature and the `TiersConfig` field names. Adjust the calls to match the code — **the code is the source of truth**, not this plan's guess.

- [ ] **Step 2: Run the preamble tests**

Run: `uv run pytest tests/test_preamble.py -v`
Expected: PASS. A failure here is a real finding: report the exact assertion and the code's actual behavior before changing anything.

- [ ] **Step 3: Write the merger tests**

Create `tests/test_merger.py`:

```python
from __future__ import annotations

from delapan.core.exploration.merger import FindingMerger, confidence_from_sources


def _raw(title, category="fact", url="http://a", content=None):
    return {"title": title, "category": category, "content": content or {"k": "v"},
            "provenance": [{"url": url}]}


def test_confidence_rises_with_source_count_and_saturates():
    assert confidence_from_sources(1) < confidence_from_sources(2) < confidence_from_sources(5)
    assert confidence_from_sources(50) <= 1.0


def test_similar_titles_in_same_category_merge_and_union_provenance():
    m = FindingMerger()
    out = m.merge_findings([
        _raw("Tavily pricing tiers", url="http://a"),
        _raw("Tavily pricing tier", url="http://b"),   # fuzzy-equal title
    ])
    assert len(out) == 1
    assert {p["url"] for p in out[0].provenance} == {"http://a", "http://b"}
    assert out[0].confidence > confidence_from_sources(1) - 0.001


def test_same_title_in_different_categories_does_not_merge():
    m = FindingMerger()
    out = m.merge_findings([_raw("Pricing", category="fact"),
                            _raw("Pricing", category="decision")])
    assert len(out) == 2


def test_unrelated_titles_do_not_merge():
    m = FindingMerger()
    out = m.merge_findings([_raw("Tavily pricing"), _raw("Postgres vector index")])
    assert len(out) == 2


def test_findings_below_min_confidence_are_dropped():
    m = FindingMerger(min_confidence=0.99)
    assert m.merge_findings([_raw("Only one source")]) == []
```

Open `delapan/core/exploration/merger.py` first: match `FindingMerger.__init__`'s real parameter names (`fuzzy_threshold`, `min_confidence`) and what `merge_findings` actually accepts (`RawFinding` objects vs dicts) — adapt the tests to the code.

- [ ] **Step 4: Run the merger tests**

Run: `uv run pytest tests/test_merger.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check tests
uv run pytest tests/test_preamble.py tests/test_merger.py -q
git add tests/test_preamble.py tests/test_merger.py
git commit -m "test: first coverage for the banding, coverage-verdict, and merge heuristics"
```

---

### Task 7: Golden query sets

§C4b. Freezes real query→finding behavior so retrieval changes are measurable, and reproduces the observed false-`rich` verdict as an executable case.

**Files:**
- Create: `scripts/gen_golden_embeddings.py`, `tests/golden/synthetic_disjoint.yaml`, `tests/golden/synthetic_disjoint.vectors.json`, `tests/test_golden_sets.py`
- Read first: `delapan/core/clients/embeddings.py` (`embed_batch`), `delapan/core/agent/preamble.py`

**Interfaces:**
- Consumes: Task 2's live-row filter; Task 6's heuristic tests.
- Produces: `tests/golden/<name>.yaml` + `<name>.vectors.json` fixtures and an offline runner; the harness Task 8 recalibrates against.

- [ ] **Step 1: Define the fixture format**

Create `tests/golden/synthetic_disjoint.yaml` — hand-written, deterministic vectors (no API needed for this set):

```yaml
# Golden set: two disjoint topics. A query about topic A must not band topic B's
# findings, and an unrelated query must return `gap`.
# Vectors live in the sidecar (synthetic_disjoint.vectors.json) keyed by id.
name: synthetic_disjoint
embedding_model: synthetic  # hand-built unit vectors; no live model involved
findings:
  - id: a1
    title: Tavily search pricing
    content: Free tier is 1000 requests per month.
    category: fact
  - id: a2
    title: Tavily search depth
    content: advanced mode costs 2 credits per query.
    category: fact
  - id: b1
    title: Postgres vector index
    content: HNSW is the default pgvector index.
    category: fact
queries:
  - query: what does tavily cost
    expect_verdict: sparse
    expect_top_ids: [a1, a2]
  - query: unrelated to anything in this kb
    expect_verdict: gap
    expect_top_ids: []
```

- [ ] **Step 2: Write the vectors sidecar**

Create `tests/golden/synthetic_disjoint.vectors.json`. Use 1536-dim vectors built from a tiny basis so similarities are exact and obvious: topic-A items point along axis 0, topic-B along axis 1, the unrelated query along axis 2.

Generate it once with this snippet (run it, commit the output, do not commit the snippet):

```python
import json

def vec(axis, jitter=0.0):
    v = [0.0] * 1536
    v[axis] = 1.0
    if jitter:
        v[axis] = 1.0 - jitter
        v[axis + 10] = jitter
    return v

json.dump(
    {
        "a1": vec(0), "a2": vec(0, 0.15), "b1": vec(1),
        "q:what does tavily cost": vec(0, 0.35),
        "q:unrelated to anything in this kb": vec(2),
    },
    open("tests/golden/synthetic_disjoint.vectors.json", "w"),
)
```

Queries are keyed `q:<query text>`; findings by their id.

- [ ] **Step 3: Write the runner**

Create `tests/test_golden_sets.py`:

```python
"""Golden query sets — freeze query→verdict behavior with recorded vectors.

    fixture yaml + vectors.json ─► seed temp store ─► match_findings
                                                  └─► band → assess_coverage

Fully offline: query vectors are recorded, so no embedding client is involved.
A failure means retrieval behavior moved — inspect before re-recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from delapan.core.agent.preamble import assess_coverage, band_findings
from delapan.core.config import TiersConfig

GOLDEN = Path(__file__).parent / "golden"


def _sets():
    return sorted(GOLDEN.glob("*.yaml"))


@pytest.mark.parametrize("path", _sets(), ids=lambda p: p.stem)
@pytest.mark.asyncio
async def test_golden_set(path, store):
    spec = yaml.safe_load(path.read_text())
    vectors = json.loads(path.with_suffix(".vectors.json").read_text())

    org, pid = store.resolve_project(f"golden-{spec['name']}", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [
            {
                "id": f["id"], "org_id": org, "kb_id": kb, "title": f["title"],
                "content": f["content"], "category": f.get("category", "fact"),
                "confidence": 0.5, "tags": [], "provenance": [],
                "embedding": vectors[f["id"]],
            }
            for f in spec["findings"]
        ]
    )

    cfg = TiersConfig()
    for q in spec["queries"]:
        hits = await store.match_findings(
            kb, vectors[f"q:{q['query']}"], match_count=20, min_similarity=cfg.band3_min
        )
        bands = band_findings(hits, cfg)
        verdict = assess_coverage(bands, cfg)
        assert verdict == q["expect_verdict"], f"{path.stem}/{q['query']}: {verdict}"
        banded = [r["id"] for b in (1, 2, 3) for r in bands[b]]
        assert banded[: len(q["expect_top_ids"])] == q["expect_top_ids"], (
            f"{path.stem}/{q['query']}: {banded}"
        )
```

Add `pyyaml` to the dev deps in `pyproject.toml` if `import yaml` fails:

```toml
dev = ["pytest>=8.4.2", "pytest-asyncio>=1.3.0", "ruff>=0.12.0", "pyright>=1.1.409", "pyyaml>=6.0"]
```

- [ ] **Step 4: Run the synthetic set**

Run: `uv run pytest tests/test_golden_sets.py -v`
Expected: PASS. If the verdicts disagree with the fixture, first check whether the *fixture's* expectation is right under the current `TiersConfig` — fix the fixture, not the engine.

- [ ] **Step 5: Write the recorder for real-model sets**

Create `scripts/gen_golden_embeddings.py`:

```python
"""Record embeddings for a golden set — findings AND queries — into its sidecar.

    <name>.yaml ──► embed_batch(findings + queries) ──► <name>.vectors.json

Run once per set (needs AI_GATEWAY_API_KEY); the committed vectors keep the
test suite offline. Re-run only when deliberately re-recording against a new
embedding model — then update `embedding_model:` in the yaml too.

    uv run python scripts/gen_golden_embeddings.py tests/golden/delapan_engine.yaml
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml

from delapan.core.clients.embeddings import embed_batch


async def main(path_str: str) -> None:
    path = Path(path_str)
    spec = yaml.safe_load(path.read_text())
    findings = spec["findings"]
    queries = [q["query"] for q in spec["queries"]]

    texts = [f"{f['title']}\n\n{f['content']}" for f in findings] + queries
    vecs = await embed_batch(texts)

    out: dict[str, list[float]] = {}
    for f, v in zip(findings, vecs[: len(findings)]):
        out[f["id"]] = [round(x, 6) for x in v]
    for q, v in zip(queries, vecs[len(findings) :]):
        out[f"q:{q}"] = [round(x, 6) for x in v]

    sidecar = path.with_suffix(".vectors.json")
    sidecar.write_text(json.dumps(out))
    print(f"wrote {sidecar} ({len(out)} vectors)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 6: Build the `delapan_engine` set from real KB findings**

This set reproduces the observed bug: a query about delapan's architecture returned FDE-career findings under a `rich` verdict.

```bash
uv run python - <<'PY'
import asyncio, json
from delapan.store import get_store
async def main():
    s = get_store()
    org, pid = s.resolve_project("delapan", create=False)
    kb = s.resolve_kb(org, pid, "master", create=False)
    rows = s.list_findings(kb, limit=12)["findings"]
    for r in rows:
        print(r["id"], "|", r["title"])
asyncio.run(main())
PY
```

Create `tests/golden/delapan_engine.yaml` using a handful of those real titles/bodies (keep bodies short — one or two sentences each; they are fixtures, not archives). Include a mix: several off-topic (FDE-career) findings and, if present, on-topic engine findings. Add:

```yaml
name: delapan_engine
embedding_model: google/gemini-embedding-001
```

and one query — `delapan engine architecture: findings pipeline, preamble assembly, coverage banding` — with the verdict the bands **should** produce (`sparse` or `gap` when only off-topic findings exist). Then record its vectors:

```bash
uv run python scripts/gen_golden_embeddings.py tests/golden/delapan_engine.yaml
```

- [ ] **Step 7: Run both sets — expect the delapan_engine set to FAIL**

Run: `uv run pytest tests/test_golden_sets.py -v`
Expected: `synthetic_disjoint` PASSES; `delapan_engine` **FAILS** with `rich != sparse`. **This failure is the deliverable** — it is the miscalibration bug, now executable. Mark it expected so the suite stays green until Task 8 fixes it:

```python
@pytest.mark.parametrize(
    "path",
    [
        pytest.param(
            p,
            marks=pytest.mark.xfail(
                p.stem == "delapan_engine",
                reason="bands tuned for text-embedding-3-small; recalibrated in Task 8",
                strict=True,
            ),
        )
        for p in _sets()
    ],
    ids=lambda p: p.stem,
)
```

- [ ] **Step 8: Run and commit**

Run: `uv run pytest tests/test_golden_sets.py -v`
Expected: 1 passed, 1 xfailed.

```bash
uv run ruff check scripts tests
git add scripts/gen_golden_embeddings.py tests/golden tests/test_golden_sets.py pyproject.toml
git commit -m "test(golden): offline query→verdict sets; delapan_engine xfails on the band bug"
```

---

### Task 8: Recalibrate the coverage bands for the live embedding model

§C4c. The bands (`.55/.40/.25`) were tuned for `text-embedding-3-small`; the engine now embeds with `gemini-embedding-001`, whose similarity distribution differs — that is why "rich" fires on off-topic hits.

**Files:**
- Create: `scripts/calibrate_bands.py`
- Modify: `config.yaml` (`tiers:` thresholds + `memory.enabled`)
- Modify: `tests/test_golden_sets.py` (drop the xfail)

**Interfaces:**
- Consumes: Task 7's golden sets.
- Produces: recalibrated `tiers` thresholds in `config.yaml`; `memory.enabled: true` (local tier; cloud still guarded by Task 5).

- [ ] **Step 1: Write the calibration script**

Create `scripts/calibrate_bands.py`:

```python
"""Propose coverage-band thresholds for the live embedding model.

    on-topic queries ──► same-KB similarities  ─┐
                                                ├─► distributions ─► proposed bands
    same queries ─────► other-KB similarities ─┘

Bands gate QUERY→finding similarity, so that is what we sample: a KB's own
queries as the positive class, the same queries against a DIFFERENT KB as the
off-topic negative class. Output is advisory — a human picks the thresholds and
the golden sets prove them.

    uv run python scripts/calibrate_bands.py delapan master --against actuary/ifrs17-hk
"""

from __future__ import annotations

import argparse
import asyncio
import statistics

from delapan.core.clients.embeddings import embed_batch
from delapan.store import get_store

DEFAULT_QUERIES = [
    "delapan engine architecture: findings pipeline and preamble assembly",
    "how does coverage banding decide rich vs sparse vs gap",
    "what orchestration patterns did production systems converge on",
]


async def _sims(store, kb_id: str, embs: list[list[float]]) -> list[float]:
    out: list[float] = []
    for e in embs:
        hits = await store.match_findings(kb_id, e, match_count=20, min_similarity=0.0)
        out.extend(h["similarity"] for h in hits)
    return out


def _describe(label: str, sims: list[float]) -> None:
    if not sims:
        print(f"{label}: (no hits)")
        return
    qs = statistics.quantiles(sims, n=100)
    print(
        f"{label}: n={len(sims)} min={min(sims):.3f} p25={qs[24]:.3f} "
        f"median={statistics.median(sims):.3f} p75={qs[74]:.3f} p95={qs[94]:.3f} max={max(sims):.3f}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("kb")
    ap.add_argument("--against", required=True, help="off-topic KB as project/kb")
    ap.add_argument("--query", action="append", default=None)
    args = ap.parse_args()

    store = get_store()
    queries = args.query or DEFAULT_QUERIES
    embs = await embed_batch(queries)

    org, pid = store.resolve_project(args.project, create=False)
    kb_id = store.resolve_kb(org, pid, args.kb, create=False)
    off_project, off_kb = args.against.split("/", 1)
    o_org, o_pid = store.resolve_project(off_project, create=False)
    off_kb_id = store.resolve_kb(o_org, o_pid, off_kb, create=False)

    on = await _sims(store, kb_id, embs)
    off = await _sims(store, off_kb_id, embs)
    _describe("on-topic (positive)", on)
    _describe("off-topic (negative)", off)

    if on and off:
        off_p95 = statistics.quantiles(off, n=100)[94]
        on_median = statistics.median(on)
        print("\nproposed (advisory):")
        print(f"  band1_min: {max(off_p95, on_median):.2f}   # above off-topic noise")
        print(f"  band2_min: {off_p95:.2f}   # off-topic p95 — the separation point")
        print(f"  band3_min: {(off_p95 * 0.8):.2f}   # weak-but-plausible floor")
        print("\nValidate with: uv run pytest tests/test_golden_sets.py -v")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the calibration**

```bash
cd /Users/anthonysuherli/Projects/delapan
uv run python scripts/calibrate_bands.py delapan master --against actuary/ifrs17-hk
```

Expected: two distributions plus advisory thresholds. If `actuary/ifrs17-hk` is unavailable locally, pick any other populated KB (`delapan_projects` lists them) — the negative class just needs a genuinely different topic.

- [ ] **Step 3: Set the thresholds**

In `config.yaml`, under `tiers:` — use the script's proposal, rounded, and record what they were calibrated against:

```yaml
tiers:
  # Calibrated for google/gemini-embedding-001 (scripts/calibrate_bands.py,
  # 2026-07-16, delapan/master vs actuary/ifrs17-hk). Re-run when the
  # embedding model changes — thresholds do not transfer across models.
  band1_min: <proposed>
  band2_min: <proposed>
  band3_min: <proposed>
  rich_hit_count: 3
  preamble_char_budget: 7000
```

Replace `<proposed>` with the script's actual numbers. Keep `rich_hit_count` and `preamble_char_budget` as they are.

- [ ] **Step 4: Prove the fix with the golden set**

Remove the `xfail` mark from `tests/test_golden_sets.py`, restoring the plain parametrize:

```python
@pytest.mark.parametrize("path", _sets(), ids=lambda p: p.stem)
```

Note: `TiersConfig()` in the test uses code defaults, not `config.yaml`. Make the runner read the real config so the recalibration is what's under test — replace `cfg = TiersConfig()` with:

```python
    from delapan.core.config import get_config

    get_config.cache_clear()
    cfg = get_config().tiers
```

- [ ] **Step 5: Run the golden sets**

Run: `uv run pytest tests/test_golden_sets.py -v`
Expected: both sets PASS — the `delapan_engine` query now returns `sparse`/`gap` instead of `rich`. If it still says `rich`, the thresholds are too low: raise `band1_min` toward the on-topic median and re-run.

- [ ] **Step 6: Enable resolution on the local tier**

In `config.yaml`:

```yaml
memory:
  enabled: true  # local tier; cloud stays pure-ADD behind the persist guard until parity
```

Leave the `MemoryConfig.enabled` code default `False` — a missing yaml key should stay dark. Update the test from Task 1 to assert the yaml-loaded value:

```python
def test_memory_resolution_enabled_via_yaml():
    from delapan.core.config import get_config

    get_config.cache_clear()
    assert get_config().memory.enabled is True


def test_memory_code_default_stays_dark():
    from delapan.core.config import MemoryConfig

    assert MemoryConfig().enabled is False
```

(Replace `test_memory_resolution_disabled_by_default` with these two.)

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check delapan scripts tests
git add -A
git commit -m "fix(tiers): recalibrate coverage bands for gemini-embedding-001; enable resolution locally"
```

---

### Task 9: Backfill script

§C5. Retires the duplicates already in KBs — same decision logic as the forward path, different mechanics (a backfill candidate already **is** a live row).

**Files:**
- Modify: `delapan/core/memory/resolver.py` (`resolve` gains an optional `neighbor_sets`)
- Create: `scripts/dedup_backfill.py`, `scripts/__init__.py`
- Test: `tests/test_backfill.py` (create), `tests/test_memory_resolver_prompt.py` (extend)

**Interfaces:**
- Consumes: Task 4's primitives; Task 5's op semantics.
- Produces: `resolve(store, kb_id, candidates, embeddings, cfg, *, neighbor_sets=None)` — when `neighbor_sets` is given, the internal `match_findings` lookup is skipped and those neighbors are used verbatim; `scripts/dedup_backfill.py` with `plan_ops(store, kb_id, cfg) -> list[BackfillOp]` and `apply_ops(store, kb_id, ops) -> dict`, both importable for tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backfill.py`:

```python
from __future__ import annotations

import pytest

from delapan.core.memory.models import ResolutionDecision, ResolutionOp

import scripts.dedup_backfill as bf


async def _seed(store, kb, title, url, emb_axis=0.01):
    return (
        await store.insert_findings(
            [
                {
                    "kb_id": kb, "title": title, "content": f"body of {title}",
                    "category": "fact", "confidence": 0.4, "tags": [],
                    "provenance": [{"url": url}], "embedding": [emb_axis] * 1536,
                }
            ]
        )
    )[0]


@pytest.mark.asyncio
async def test_neighbors_exclude_self_and_later_rows(store, monkeypatch):
    org, pid = store.resolve_project("bfA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    first = await _seed(store, kb, "Tavily pricing", "http://a")
    await _seed(store, kb, "Tavily pricing", "http://b")   # second, same topic

    seen: list[list[str]] = []

    async def _spy(store_, kb_, cands, embs, cfg, *, neighbor_sets=None):
        seen.append([n["id"] for n in (neighbor_sets or [[]])[0]])
        return [ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]

    monkeypatch.setattr(bf, "resolve", _spy)
    await bf.plan_ops(store, kb, _cfg())

    # The oldest row has nothing before it; the second sees exactly the first —
    # never itself (which would match at sim ~1.0) and never a later row.
    assert seen[0] == []
    assert seen[1] == [first]


@pytest.mark.asyncio
async def test_apply_noop_retires_the_duplicate_and_keeps_urls_live(store, monkeypatch):
    org, pid = store.resolve_project("bfB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    keeper = await _seed(store, kb, "Tavily pricing", "http://a")
    dupe = await _seed(store, kb, "Tavily pricing", "http://b")

    ops = [bf.BackfillOp(op="NOOP", candidate_id=dupe, target_id=keeper, reason="dup")]
    await bf.apply_ops(store, kb, ops)

    assert store.count_findings(kb) == 1                       # shrank
    assert store.get_finding(kb, dupe)["superseded_by"] == keeper
    urls = {p["url"] for p in store.get_finding(kb, keeper)["provenance"]}
    assert urls == {"http://a", "http://b"}                    # no url lost from live


@pytest.mark.asyncio
async def test_apply_update_makes_candidate_survivor_with_merged_urls(store):
    org, pid = store.resolve_project("bfC", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    old = await _seed(store, kb, "Pricing v1", "http://a")
    newer = await _seed(store, kb, "Pricing v2", "http://b")

    await bf.apply_ops(store, kb, [bf.BackfillOp(op="UPDATE", candidate_id=newer,
                                                 target_id=old, reason="refines")])

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, old)["superseded_by"] == newer   # old retired
    urls = {p["url"] for p in store.get_finding(kb, newer)["provenance"]}
    assert urls == {"http://a", "http://b"}                       # union survives live


@pytest.mark.asyncio
async def test_apply_supersede_keeps_only_candidate_provenance(store):
    org, pid = store.resolve_project("bfD", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    old = await _seed(store, kb, "Tavily is free", "http://old")
    newer = await _seed(store, kb, "Tavily is paid", "http://new")

    await bf.apply_ops(store, kb, [bf.BackfillOp(op="SUPERSEDE", candidate_id=newer,
                                                 target_id=old, reason="contradicts")])

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, old)["superseded_by"] == newer
    urls = {p["url"] for p in store.get_finding(kb, newer)["provenance"]}
    assert urls == {"http://new"}         # contradicted sources do NOT merge in


@pytest.mark.asyncio
async def test_apply_add_is_a_no_op(store):
    org, pid = store.resolve_project("bfE", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    fid = await _seed(store, kb, "Unique", "http://a")
    snapshot = store.get_finding(kb, fid)

    await bf.apply_ops(store, kb, [bf.BackfillOp(op="ADD", candidate_id=fid, target_id=None)])

    assert store.count_findings(kb) == 1
    assert store.get_finding(kb, fid) == snapshot   # the row already exists; keep it


def _cfg():
    from delapan.core.config import get_config

    get_config.cache_clear()
    return get_config()
```

Fix the contradictory assertions in the first test while writing it: the oldest row sees no neighbors (`seen[0] == []`); the second row sees exactly `[first]` and never itself. Delete the `assert first not in seen[1]` line — it contradicts the next assertion.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.dedup_backfill`.

- [ ] **Step 3: Make `scripts/` importable**

Create `scripts/__init__.py` (empty) if it does not exist, so tests can import the module.

- [ ] **Step 4: Let `resolve()` accept a caller-supplied neighbor set**

The backfill's whole correctness rests on restricting neighbors, but `resolve()` looks them up itself via `match_findings` — which returns the candidate's own row at similarity ~1.0. Add the seam in `delapan/core/memory/resolver.py`:

```python
async def resolve(
    store: Store,
    kb_id: str,
    candidates: list[Finding],
    embeddings: list[list[float]],
    cfg: MemoryConfig,
    *,
    neighbor_sets: list[list[dict]] | None = None,
) -> list[ResolutionDecision]:
    """One decision per candidate (same order + length as ``candidates``).

    Neighbors are retrieved per candidate unless the caller supplies
    ``neighbor_sets`` — the backfill does, because a replayed candidate is
    already a live row and would otherwise match itself.

    Candidates with no neighbor above the similarity floor short-circuit to ADD
    without consuming an LLM slot. need-LLM candidates are chunked by
    ``cfg.max_candidates_per_pass`` (one LLM call per chunk); any chunk failure →
    those candidates stay ADD. The ``cfg.enabled`` kill-switch is enforced upstream
    in resolve_and_persist, not here."""
    if not candidates:
        return []

    if neighbor_sets is None:
        neighbor_sets = []
        for emb in embeddings:
            try:
                hits = await store.match_findings(
                    kb_id,
                    emb,
                    match_count=cfg.neighbor_top_k,
                    min_similarity=cfg.neighbor_min_similarity,
                )
            except Exception:  # noqa: BLE001 — retrieval failure → treat as no neighbors
                hits = []
            neighbor_sets.append(hits)
```

Leave the rest of the function unchanged.

- [ ] **Step 5: Test the seam**

Append to `tests/test_memory_resolver_prompt.py`:

```python
@pytest.mark.asyncio
async def test_resolve_uses_supplied_neighbors_without_searching(monkeypatch):
    from delapan.core.config import MemoryConfig
    from delapan.core.memory.resolver import resolve

    class _NoSearchStore:
        async def match_findings(self, *a, **k):
            raise AssertionError("must not search when neighbor_sets is supplied")

    f = Finding(exploration_id="e", project_id="p", category="fact", title="T",
                content={"k": "v"})
    out = await resolve(_NoSearchStore(), "kb1", [f], [[0.01] * 1536],
                        MemoryConfig(), neighbor_sets=[[]])
    assert out[0].op.value == "ADD"          # no neighbors → short-circuit, no LLM
```

Add `import pytest` and the `Finding` import at the top of that file if missing.

Run: `uv run pytest tests/test_memory_resolver_prompt.py -v`
Expected: PASS.

- [ ] **Step 6: Write the script**

Create `scripts/dedup_backfill.py`:

```python
"""Retire the duplicates a KB already accumulated — same decisions, different mechanics.

    live findings (oldest→newest) ─► resolve vs EARLIER rows ─► [BackfillOp] ─► apply
                                                                    │
                        dry-run by default: print ops + cost, touch nothing

A backfill candidate is already a live row, so the forward applier does not fit:
ADD keeps the row (no insert), NOOP/UPDATE/SUPERSEDE retire the loser in place via
invalidate_finding. Neighbors are restricted to rows EARLIER in replay order and
still live — otherwise every candidate matches itself at similarity ~1.0 — and
handed to resolve() via its neighbor_sets seam.

    uv run python scripts/dedup_backfill.py delapan master           # dry run
    uv run python scripts/dedup_backfill.py delapan master --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import AppConfig, get_config
from delapan.core.exploration.merger import confidence_from_sources
from delapan.core.memory.models import ResolutionEvent, ResolutionOp
from delapan.core.memory.persist import _distinct_urls, _merge_provenance
from delapan.core.memory.resolver import resolve
from delapan.store import Store, get_store

logger = logging.getLogger(__name__)


@dataclass
class BackfillOp:
    op: str                       # ADD | UPDATE | NOOP | SUPERSEDE
    candidate_id: str
    target_id: str | None = None
    reason: str = ""


def _restrict(hits: list[dict], candidate_id: str, allowed: set[str]) -> list[dict]:
    """Keep only earlier-in-replay, still-live neighbors — never the candidate itself."""
    return [h for h in hits if h["id"] != candidate_id and h["id"] in allowed]


async def plan_ops(store: Store, kb_id: str, cfg: AppConfig) -> list[BackfillOp]:
    """Decide one op per live finding, oldest first. Reads only — never writes."""
    rows = store.list_findings(kb_id, limit=10_000)["findings"]
    rows = sorted(rows, key=lambda r: (r.get("created_at") or "", r["id"]))
    seen: set[str] = set()
    ops: list[BackfillOp] = []

    for row in rows:
        text = f"{row['title']}\n\n{row['content']}"
        emb = (await embed_batch([text]))[0]
        hits = await store.match_findings(
            kb_id, emb, match_count=cfg.memory.neighbor_top_k + 5,
            min_similarity=cfg.memory.neighbor_min_similarity,
        )
        neighbors = _restrict(hits, row["id"], seen)[: cfg.memory.neighbor_top_k]
        if not neighbors:
            ops.append(BackfillOp(op="ADD", candidate_id=row["id"], reason="no earlier neighbor"))
            seen.add(row["id"])
            continue

        candidate = _as_finding(row)
        decisions = await resolve(
            store, kb_id, [candidate], [emb], cfg.memory, neighbor_sets=[neighbors]
        )
        d = decisions[0]
        ops.append(
            BackfillOp(
                op=d.op.value, candidate_id=row["id"],
                target_id=d.target_finding_id, reason=d.reason,
            )
        )
        # Only a surviving row can be a neighbor for later candidates.
        if d.op == ResolutionOp.ADD:
            seen.add(row["id"])
        elif d.op in (ResolutionOp.UPDATE, ResolutionOp.SUPERSEDE):
            seen.discard(d.target_finding_id or "")
            seen.add(row["id"])
        # NOOP: the candidate retires; the target stays in `seen`.
    return ops


async def apply_ops(store: Store, kb_id: str, ops: list[BackfillOp]) -> dict:
    """Apply planned ops. ADD keeps the row; every other op retires one row."""
    counts: dict[str, int] = {}
    events: list[ResolutionEvent] = []
    for op in ops:
        counts[op.op] = counts.get(op.op, 0) + 1
        if op.op == "ADD" or not op.target_id:
            continue
        candidate = store.get_finding(kb_id, op.candidate_id)
        try:
            target = store.get_finding(kb_id, op.target_id)
        except Exception:  # noqa: BLE001 — target already retired by an earlier op
            logger.debug("backfill: target %s gone; skipping", op.target_id)
            continue

        if op.op == "NOOP":
            # The target survives and absorbs the duplicate's sources.
            merged = _merge_provenance(
                target.get("provenance") or [], candidate.get("provenance") or []
            )
            await store.update_finding(
                kb_id, op.target_id,
                confidence=max(
                    target.get("confidence") or 0.0,
                    confidence_from_sources(_distinct_urls(merged) or 1),
                ),
                provenance=merged,
            )
            await store.invalidate_finding(kb_id, op.candidate_id, superseded_by=op.target_id)
        elif op.op == "UPDATE":
            # The candidate (newer) survives and inherits the target's sources.
            merged = _merge_provenance(
                target.get("provenance") or [], candidate.get("provenance") or []
            )
            await store.update_finding(
                kb_id, op.candidate_id,
                confidence=confidence_from_sources(_distinct_urls(merged) or 1),
                provenance=merged,
            )
            await store.invalidate_finding(kb_id, op.target_id, superseded_by=op.candidate_id)
        elif op.op == "SUPERSEDE":
            # Contradiction: the target retires, its sources do NOT merge in.
            await store.invalidate_finding(kb_id, op.target_id, superseded_by=op.candidate_id)

        events.append(
            ResolutionEvent(
                op=op.op, candidate_title=candidate.get("title") or "",
                target_finding_id=op.target_id, new_finding_id=op.candidate_id,
                reason=f"backfill: {op.reason}",
            )
        )
    if events:
        await store.insert_resolution_events(kb_id, [e.model_dump() for e in events])
    return counts


def _as_finding(row: dict):
    from delapan.core.exploration.models import Finding

    return Finding(
        exploration_id="backfill", project_id="backfill",
        category=row.get("category") or "fact", title=row["title"],
        content=row["content"], provenance=row.get("provenance") or [],
    )


def _estimate(n_rows: int, cfg: AppConfig) -> str:
    passes = max(1, n_rows)
    return (
        f"~{n_rows} embedding calls (batched) + up to ~{passes} resolution LLM calls "
        f"({cfg.memory.resolution_model})"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="Retire duplicate findings in an existing KB.")
    ap.add_argument("project")
    ap.add_argument("kb")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    get_config.cache_clear()
    cfg = get_config()
    store = get_store()
    org, pid = store.resolve_project(args.project, create=False)
    kb_id = store.resolve_kb(org, pid, args.kb, create=False)

    before = store.count_findings(kb_id)
    print(f"{args.project}/{args.kb}: {before} live findings")
    print(f"estimated cost: {_estimate(before, cfg)}\n")

    ops = await plan_ops(store, kb_id, cfg)
    counts: dict[str, int] = {}
    for op in ops:
        counts[op.op] = counts.get(op.op, 0) + 1
    print("planned ops:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for op in ops:
        if op.op != "ADD":
            print(f"  {op.op:<9} {op.candidate_id[:8]} → {(op.target_id or '')[:8]}  {op.reason}")

    if not args.apply:
        print(f"\ndry run — nothing written. Re-run with --apply to execute.")
        return

    applied = await apply_ops(store, kb_id, ops)
    after = store.count_findings(kb_id)
    print(f"\napplied: {applied}")
    print(f"live findings: {before} → {after}")


if __name__ == "__main__":
    asyncio.run(main())
```

Cloud auth note for the docstring: on the cloud tier `get_store()` needs a token — if `active_backend() == "cloud"`, log in via `delapan.mcp.tenancy`'s flow before building the store. Add that guard when the cloud tier lands (Task 10); local tier is auth-less.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: all PASS.

- [ ] **Step 8: Dry-run against the real KB**

```bash
uv run python scripts/dedup_backfill.py delapan master
```

Expected: prints the live count, a cost estimate, and a plausible op mix with **no self-matches** (never `X → X`). Nothing is written. Report the op mix — do not `--apply` without the user's review (spec §Rollout step 6).

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check delapan scripts tests
uv run pytest -q
git add -A
git commit -m "feat(scripts): dedup backfill — replay a KB through the resolver, dry-run by default"
```

---

### Task 10: SupabaseStore parity + the cloud migration

§C6. The resolution branch predates `SupabaseStore`, so the cloud tier has none of the write primitives. Until this lands, Task 5's guard keeps cloud on pure ADD.

**Files:**
- Modify: `delapan/store/supabase.py`
- Modify: `delapan/core/memory/persist.py` (remove the cloud guard)
- Create: `migrations/2026-07-16-write-path-dedup.sql`
- Test: `tests/test_supabase_resolution.py` (create)
- Read first: `tests/fake_supabase.py`, `tests/test_supabase_findings.py`

**Interfaces:**
- Consumes: Task 4's Protocol contracts; Task 3's event columns.
- Produces: `SupabaseStore.{update_finding, invalidate_finding, supersede_finding, insert_resolution_events, list_resolution_events}`; `list_findings(include_invalidated=False)`; live-only `count_findings`; the SQL migration file.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supabase_resolution.py`, modeled on `tests/test_supabase_findings.py` (read it first for the fake-client fixture pattern):

```python
from __future__ import annotations

import pytest

from tests.fake_supabase import FakeClient  # match the real name used in test_supabase_findings.py


@pytest.fixture()
def cloud(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda token: fake)
    from delapan.store.supabase import SupabaseStore

    return SupabaseStore("tok", org_id="org1"), fake


@pytest.mark.asyncio
async def test_update_finding_omits_none_fields(cloud):
    store, fake = cloud
    fake.seed("findings", [{"id": "f1", "kb_id": "kb1", "org_id": "org1", "title": "T",
                            "content": "keep", "confidence": 0.4, "provenance": []}])
    await store.update_finding("kb1", "f1", confidence=0.64, provenance=[{"url": "http://b"}])
    row = fake.rows("findings")[0]
    assert row["content"] == "keep"      # None → not in the payload at all
    assert row["confidence"] == 0.64


@pytest.mark.asyncio
async def test_invalidate_finding_stamps_pointer(cloud):
    store, fake = cloud
    fake.seed("findings", [{"id": "f1", "kb_id": "kb1", "org_id": "org1",
                            "invalidated_at": None, "superseded_by": None}])
    await store.invalidate_finding("kb1", "f1", superseded_by="f2")
    row = fake.rows("findings")[0]
    assert row["invalidated_at"]
    assert row["superseded_by"] == "f2"


@pytest.mark.asyncio
async def test_supersede_calls_rpc_with_enriched_row(cloud):
    store, fake = cloud
    captured = {}
    fake.register_rpc("supersede_finding", lambda params: captured.update(params) or "new1")
    new_id = await store.supersede_finding(
        "kb1", "old1",
        {"kb_id": "kb1", "title": "T", "content": "c", "category": "fact",
         "confidence": 0.6, "tags": [], "provenance": [{"url": "http://a"}],
         "embedding": [0.1] * 1536},
    )
    assert new_id == "new1"
    row = captured["p_row"]
    assert row["org_id"] == "org1"           # enriched like insert_findings
    assert row["status"] == "approved"
    assert row["valid_from"]
    assert row["embedding"].startswith("[")  # vector text, not a list


@pytest.mark.asyncio
async def test_count_and_list_hide_invalidated(cloud):
    store, fake = cloud
    fake.seed("findings", [
        {"id": "live", "kb_id": "kb1", "org_id": "org1", "invalidated_at": None,
         "title": "A", "content": "x", "category": "fact", "confidence": 0.5,
         "created_at": "2026-01-01"},
        {"id": "dead", "kb_id": "kb1", "org_id": "org1", "invalidated_at": "2026-07-16",
         "title": "B", "content": "y", "category": "fact", "confidence": 0.5,
         "created_at": "2026-01-02"},
    ])
    assert store.count_findings("kb1") == 1
    assert [f["id"] for f in store.list_findings("kb1")["findings"]] == ["live"]
    assert len(store.list_findings("kb1", include_invalidated=True)["findings"]) == 2
```

Adapt fixture/helper names to whatever `tests/fake_supabase.py` actually exposes (`seed`/`rows`/`register_rpc` may be named differently — the fake is the source of truth). If the fake cannot express `is null` filters, extend it minimally rather than weakening the test.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_supabase_resolution.py -v`
Expected: FAIL — `AttributeError: 'SupabaseStore' object has no attribute 'update_finding'`.

- [ ] **Step 3: Implement the write primitives on SupabaseStore**

In `delapan/store/supabase.py`, add after `insert_findings`:

```python
    def _row_payload(self, r: dict) -> dict:
        """The insert_findings enrichment, shared with supersede_finding."""
        row = {
            "id": r.get("id") or uuid.uuid4().hex, "org_id": self._org_id,
            "kb_id": r.get("kb_id"), "title": r.get("title"), "content": r.get("content"),
            "category": r.get("category"), "confidence": r.get("confidence"),
            "tags": list(r.get("tags") or []), "provenance": list(r.get("provenance") or []),
            "status": "approved", "created_at": r.get("created_at") or _now_iso(),
            "valid_from": r.get("valid_from") or _now_iso(),
        }
        emb = r.get("embedding")
        if emb is not None:
            row["embedding"] = self._vec(list(emb))
        return row

    async def update_finding(
        self, kb_id: str, finding_id: str, *, content=None, confidence=None,
        provenance=None, embedding=None, title: str | None = None,
    ) -> None:
        """Partial in-place update; ``None`` fields are left out of the payload."""
        patch: dict = {}
        if content is not None:
            patch["content"] = content
        if confidence is not None:
            patch["confidence"] = confidence
        if provenance is not None:
            patch["provenance"] = list(provenance)
        if title is not None:
            patch["title"] = title
        if embedding is not None:
            patch["embedding"] = self._vec(list(embedding))
        if not patch:
            return
        await asyncio.to_thread(
            lambda: self._c.table("findings").update(patch)
            .eq("kb_id", kb_id).eq("id", finding_id).execute()
        )

    async def invalidate_finding(
        self, kb_id: str, finding_id: str, *, superseded_by: str | None = None
    ) -> None:
        """Retire a row in place — it leaves every read path but stays readable."""
        await asyncio.to_thread(
            lambda: self._c.table("findings")
            .update({"invalidated_at": _now_iso(), "superseded_by": superseded_by})
            .eq("kb_id", kb_id).eq("id", finding_id).execute()
        )

    async def supersede_finding(self, kb_id: str, target_id: str, new_row: dict) -> str:
        """Insert + retire in one server-side transaction. Returns the new id."""
        params = {
            "p_kb_id": kb_id, "p_target_id": target_id, "p_row": self._row_payload(new_row)
        }
        data = await asyncio.to_thread(
            lambda: self._c.rpc("supersede_finding", params).execute().data
        )
        return data if isinstance(data, str) else data[0]
```

Refactor `insert_findings` to build its payload with `_row_payload` so the two paths cannot drift.

Then add the events methods near the other list/read methods:

```python
    async def insert_resolution_events(self, kb_id: str, events: list[dict]) -> None:
        """Append resolution decision rows. Best-effort: an audit-log write must
        never break the caller, so failures are logged and swallowed."""
        if not events:
            return
        payload = [
            {
                "org_id": self._org_id, "kb_id": kb_id, "op": e.get("op"),
                "candidate_title": e.get("candidate_title"),
                "target_finding_id": e.get("target_finding_id"),
                "new_finding_id": e.get("new_finding_id"),
                "details": e.get("details"), "reason": e.get("reason") or "",
                "created_at": _now_iso(),
            }
            for e in events
        ]
        try:
            await asyncio.to_thread(
                lambda: self._c.table("resolution_events").insert(payload).execute()
            )
        except Exception:  # noqa: BLE001 — audit log is best-effort
            logger.warning("failed to write resolution_events for kb=%s", kb_id, exc_info=True)

    def list_resolution_events(self, kb_id: str, limit: int | None = None) -> list[dict]:
        """Most-recent resolution events for `kb_id`, newest first (cap 500)."""
        n = min(limit or 50, 500)
        return (
            self._c.table("resolution_events").select("*")
            .eq("kb_id", kb_id).order("created_at", desc=True).limit(n).execute().data
        ) or []
```

Add `import logging` + `logger = logging.getLogger(__name__)` at the top if absent.

- [ ] **Step 4: Hide retired rows on the cloud read paths**

In `SupabaseStore`, add `.is_("invalidated_at", "null")` to `count_findings` and to `list_findings` (unless `include_invalidated=True`), and extend `_finding` to carry `valid_from`, `invalidated_at`, `superseded_by`. `match_findings` needs **no client change** — its filter is server-side in the RPC.

- [ ] **Step 5: Run the cloud tests**

Run: `uv run pytest tests/test_supabase_resolution.py tests/test_supabase_findings.py -v`
Expected: all PASS.

- [ ] **Step 6: Write the migration file**

Create `migrations/2026-07-16-write-path-dedup.sql` (create the directory if needed) with the spec's §Migration SQL verbatim, plus the new RPC:

```sql
-- Write-path dedup: bi-temporal findings + resolution audit log.
-- Apply to the cloud project BEFORE removing the pure-ADD guard in persist.py.
-- Spec: docs/superpowers/specs/2026-07-16-write-path-dedup-design.md

-- valid_from: add WITHOUT a default first — Postgres backfills a column default
-- into every existing row at ALTER time, which would clobber the created_at seed.
alter table findings add column if not exists valid_from timestamptz;
update findings set valid_from = created_at where valid_from is null;
alter table findings alter column valid_from set default now();

alter table findings
  add column if not exists invalidated_at timestamptz,
  add column if not exists superseded_by uuid references findings(id) on delete set null;
create index if not exists findings_live_idx on findings (kb_id) where invalidated_at is null;

create table if not exists resolution_events (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  kb_id uuid not null,
  op text not null,
  candidate_title text not null,
  target_finding_id uuid,
  new_finding_id uuid,
  details jsonb,
  reason text default '',
  created_at timestamptz default now()
);
alter table resolution_events enable row level security;

create or replace function supersede_finding(p_kb_id uuid, p_target_id uuid, p_row jsonb)
returns uuid language plpgsql security invoker as $$
declare v_new_id uuid;
begin
  insert into findings (id, org_id, kb_id, title, content, category, confidence,
                        tags, provenance, status, created_at, valid_from, embedding)
  values (coalesce((p_row->>'id')::uuid, gen_random_uuid()),
          (p_row->>'org_id')::uuid, (p_row->>'kb_id')::uuid,
          p_row->>'title', p_row->>'content', p_row->>'category',
          (p_row->>'confidence')::real,
          coalesce(p_row->'tags', '[]'::jsonb), coalesce(p_row->'provenance', '[]'::jsonb),
          coalesce(p_row->>'status', 'approved'),
          coalesce((p_row->>'created_at')::timestamptz, now()),
          coalesce((p_row->>'valid_from')::timestamptz, now()),
          (p_row->>'embedding')::vector)
  returning id into v_new_id;

  update findings set invalidated_at = now(), superseded_by = v_new_id
   where id = p_target_id and kb_id = p_kb_id and invalidated_at is null;
  if not found then
    raise exception 'supersede target % not live in kb %', p_target_id, p_kb_id;
  end if;
  return v_new_id;
end $$;
```

Two apply-time steps that cannot be written blind (both flagged in the spec):
1. **RLS policies** — dump the `findings` table's org-scoped policies (`select * from pg_policies where tablename = 'findings'`), copy them for `resolution_events` renamed, and append the resulting `create policy` statements to this file.
2. **match_findings** — dump the deployed body (`select pg_get_functiondef(oid) from pg_proc where proname = 'match_findings'`), add `and f.invalidated_at is null` to its WHERE clause, and append the full `create or replace function` to this file.

- [ ] **Step 7: Apply the migration and verify parity**

**Stop here and hand back to the user.** Applying this touches the live cloud project (findings data). Report: the migration file is ready, the two dump-and-amend steps are outstanding, and the guard stays until a human applies it. Do not run the migration autonomously.

- [ ] **Step 8: Remove the cloud guard (only after the migration is applied and verified)**

In `delapan/core/memory/persist.py`, restore the kill-switch to the plain check:

```python
    # Kill-switch / fast path: pure ADD, no resolution, no events.
    if not cfg.memory.enabled:
```

and drop the now-unused `active_backend` import. Delete `test_cloud_tier_forces_pure_add` from `tests/test_memory_persist.py` and update `test_supabase_resolution.py` with a cloud-tier round-trip proving acceptance #4 (superseded rows never returned).

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check delapan tests
uv run pytest -q
git add -A
git commit -m "feat(store): SupabaseStore resolution parity + cloud write-path migration"
```

---

## Post-plan verification (run after Task 10)

Acceptance criteria from spec §Testing:

- [ ] `uv run pytest -q` — full suite green.
- [ ] Golden sets pass offline: `uv run pytest tests/test_golden_sets.py -v` (acceptance #2, #6).
- [ ] **Idempotence (acceptance #3)** — write `tests/test_idempotence.py`: seed a temp KB, run one candidate batch through `resolve_and_persist` twice with a **stubbed resolver** returning NOOP for the repeats; assert 0 ADDs, live `count_findings` stable, and the repeated rows byte-identical. (A live-LLM version belongs in a manual check, not CI.)
- [ ] **Kill-switch (acceptance #5)**: set `memory.enabled: false`, run an explore against a temp KB, confirm rows match pre-change master apart from the three new columns.
- [ ] **Backfill (acceptance #7)**: dry-run `delapan/master`, review the op mix with the user, then `--apply` **on a copy** (`cp ~/.delapan/delapan.db /tmp/copy.db && DELAPAN_DB_PATH=/tmp/copy.db …`) and verify live count shrank with no live URL lost except SUPERSEDE-retired ones.
