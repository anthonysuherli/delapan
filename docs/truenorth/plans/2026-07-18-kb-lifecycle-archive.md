# KB Lifecycle — Archive + Truthful Activity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reversible archive state for projects and KBs on both `Store` backends, and rebuild `list_projects` to report activity from real finding writes instead of the dead `category='snapshot'` convention.

**Architecture:** A nullable `archived_at` timestamp on `projects` and `kbs`, set through one new `Store` method (`set_archived`). Archiving is cascade-by-read — a project's archive flag hides its KBs without stamping them, so unarchive is lossless. `list_projects` gains `include_archived` and swaps `snapshot_count`/`last_activity` for `finding_count`/`last_finding_at`, computed in a single aggregate instead of the current per-KB N+1.

**Vision goals served:** *Reversible KB lifecycle* (archive/unarchive through the `Store` seam, activity from first-class writes); *Two tiers at protocol parity* (every method lands on both backends with identical return shapes).

**Tech Stack:** Python 3.11+, SQLite + sqlite-vec, Supabase (Postgres + pgvector) via supabase-py/PostgREST, FastAPI, FastMCP, pytest.

**Spec:** `docs/truenorth/specs/2026-07-18-kb-lifecycle-archive-design.md`

## Global Constraints

- **Parity is mandatory.** Every `Store` method must land on `SQLiteStore` *and* `SupabaseStore` with identical return shapes before the feature is done. A method on one tier only is an unfinished feature.
- **Non-destructive.** Archiving sets a column. It must never delete or modify a finding, node, edge, or `grounded_in` value.
- **No create-on-demand.** Unlike `resolve_project` / `resolve_kb`, `set_archived` raises on a missing project or KB.
- **Idempotent.** Archiving an already-archived target returns the existing `archived_at` unchanged — never bumps the timestamp.
- **Live findings only.** Every count and timestamp filters `invalidated_at IS NULL`, so superseded rows never inflate them.
- **Return shapes stay plain dicts / lists of dicts.** No backend-specific object crosses the `Store` seam.
- `_ORG` is the synthetic `"local"` org on the SQLite tier; `JOURNAL_SCOPE = "__journal__"` (`delapan/store/sqlite.py:49`) is excluded from listings on both tiers.
- SQLite writes require an explicit `self._conn.commit()`.
- Branch: `docs/solo-project-tracker`. Run tests with `pytest` from `/Users/anthonysuherli/projects/delapan`.

---

### Task 1: SQLite schema + `set_archived`

**Files:**
- Modify: `delapan/store/base.py:149-157` (protocol)
- Modify: `delapan/store/sqlite.py:137-151` (migration list), and append methods after `list_projects`
- Test: `tests/test_archive_sqlite.py` (create)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `Store.set_archived(*, project_id: str, kb_id: str | None = None, archived: bool) -> dict` returning `{"project_id": str, "kb_id": str | None, "archived_at": str | None, "finding_count": int}`. Task 3 mirrors this on Supabase; Tasks 5-7 consume it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_archive_sqlite.py`:

```python
from __future__ import annotations

import pytest

_ORG = "local"


def _seed(store):
    """A project with one KB → (project_id, kb_id)."""
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb(_ORG, pid, "main", create=True)
    return pid, kid


def test_archive_kb_sets_timestamp(store):
    pid, kid = _seed(store)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert out["project_id"] == pid
    assert out["kb_id"] == kid
    assert out["archived_at"] is not None
    assert out["finding_count"] == 0


def test_unarchive_clears_timestamp(store):
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    assert out["archived_at"] is None


def test_archive_is_idempotent(store):
    pid, kid = _seed(store)
    first = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    second = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert first["archived_at"] == second["archived_at"]


def test_archive_project_level(store):
    pid, _ = _seed(store)
    out = store.set_archived(project_id=pid, archived=True)
    assert out["kb_id"] is None
    assert out["archived_at"] is not None


def test_archive_unknown_raises(store):
    with pytest.raises(RuntimeError):
        store.set_archived(project_id="nope", archived=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_archive_sqlite.py -v`
Expected: FAIL — `AttributeError: 'SQLiteStore' object has no attribute 'set_archived'`

- [ ] **Step 3: Add the migration columns**

In `delapan/store/sqlite.py`, append to `_ADD_COLUMN_MIGRATIONS` (the list ending at line 151):

```python
    # 0011: reversible KB lifecycle — NULL archived_at = active.
    "ALTER TABLE projects ADD COLUMN archived_at TEXT;",
    "ALTER TABLE kbs ADD COLUMN archived_at TEXT;",
```

Also add the columns to the `CREATE TABLE` statements in the schema string so fresh DBs get them directly: add `archived_at TEXT` to both the `projects` and `kbs` table definitions.

- [ ] **Step 4: Add the protocol method**

In `delapan/store/base.py`, immediately after the `list_projects` stub (line 157):

```python
    def set_archived(
        self, *, project_id: str, kb_id: str | None = None, archived: bool
    ) -> dict:
        """Archive or unarchive a project (``kb_id=None``) or a single KB.

        Returns ``{"project_id", "kb_id", "archived_at", "finding_count"}``.
        ``finding_count`` counts live findings (``invalidated_at IS NULL``) — for
        the whole project when ``kb_id`` is None. Idempotent: archiving an
        already-archived target returns the existing stamp. Raises if the target
        does not exist — this never creates on demand.
        """
        ...
```

- [ ] **Step 5: Implement on SQLiteStore**

In `delapan/store/sqlite.py`, after `list_projects` (ends line 666):

```python
    def set_archived(
        self, *, project_id: str, kb_id: str | None = None, archived: bool
    ) -> dict:
        """Stamp/clear ``archived_at`` on a KB (or the project when kb_id is None)."""
        table, row_id = ("kbs", kb_id) if kb_id else ("projects", project_id)
        row = self._conn.execute(
            f"SELECT archived_at FROM {table} WHERE id = ? AND org_id = ?;",
            (row_id, _ORG),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"{table} {row_id!r} not found")

        current = row["archived_at"]
        if archived and current is not None:
            stamp = current  # idempotent — don't move the timestamp
        else:
            stamp = _now_iso() if archived else None
            self._conn.execute(
                f"UPDATE {table} SET archived_at = ? WHERE id = ?;", (stamp, row_id)
            )
            self._conn.commit()

        return {
            "project_id": project_id,
            "kb_id": kb_id,
            "archived_at": stamp,
            "finding_count": self._live_finding_count(project_id, kb_id),
        }

    def _live_finding_count(self, project_id: str, kb_id: str | None) -> int:
        """Live (non-invalidated) findings in one KB, or across a project's KBs."""
        if kb_id:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM findings "
                "WHERE kb_id = ? AND invalidated_at IS NULL;",
                (kb_id,),
            ).fetchone()
        else:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE invalidated_at IS NULL "
                "AND kb_id IN (SELECT id FROM kbs WHERE project_id = ?);",
                (project_id,),
            ).fetchone()
        return int(r["n"])
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_archive_sqlite.py -v`
Expected: 5 passed

- [ ] **Step 7: Verify no regression**

Run: `pytest -q`
Expected: all pass except `tests/test_supabase_tenancy.py::test_list_projects_shape` if already touched — it should still pass at this point since `list_projects` is unchanged.

- [ ] **Step 8: Commit**

```bash
git add delapan/store/base.py delapan/store/sqlite.py tests/test_archive_sqlite.py
git commit -m "feat(store): archived_at column + set_archived on SQLite tier"
```

---

### Task 2: SQLite `list_projects` rewrite

**Files:**
- Modify: `delapan/store/base.py:149-157` (docstring + signature)
- Modify: `delapan/store/sqlite.py:638-666` (replace `list_projects`)
- Test: `tests/test_archive_sqlite.py` (extend)

**Interfaces:**
- Consumes: `set_archived` from Task 1.
- Produces: `list_projects(*, include_archived: bool = False) -> list[dict]` returning `[{"project", "project_id", "archived_at", "kbs": [{"kb", "kb_id", "finding_count", "last_finding_at", "archived_at"}]}]`. Task 4 mirrors it; Tasks 6-8 consume it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_archive_sqlite.py`:

```python
async def test_list_projects_reports_real_finding_activity(store):
    pid, kid = _seed(store)
    await store.insert_findings([{
        "org_id": _ORG, "kb_id": kid, "title": "t", "content": "c",
        "category": "anything-at-all", "confidence": 1.0,
        "tags": [], "provenance": {}, "embedding": [0.0] * 8,
    }])
    [proj] = store.list_projects()
    [kb] = proj["kbs"]
    assert kb["finding_count"] == 1
    assert kb["last_finding_at"] is not None
    assert "snapshot_count" not in kb
    assert "last_activity" not in kb


async def test_superseded_findings_do_not_inflate_the_count(store):
    """Bi-temporal exclusion — retired rows must not count as activity."""
    pid, kid = _seed(store)
    [fid] = await store.insert_findings([{
        "org_id": _ORG, "kb_id": kid, "title": "t", "content": "c",
        "category": "x", "confidence": 1.0,
        "tags": [], "provenance": {}, "embedding": [0.0] * 8,
    }])
    await store.invalidate_finding(kid, fid)
    [proj] = store.list_projects()
    assert proj["kbs"][0]["finding_count"] == 0
    assert store.set_archived(
        project_id=pid, kb_id=kid, archived=True
    )["finding_count"] == 0


def test_archived_kb_hidden_by_default(store):
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    [proj] = store.list_projects()
    assert proj["kbs"] == []
    [proj_all] = store.list_projects(include_archived=True)
    assert proj_all["kbs"][0]["archived_at"] is not None


def test_archived_project_hidden_by_default(store):
    pid, _ = _seed(store)
    store.set_archived(project_id=pid, archived=True)
    assert store.list_projects() == []
    assert len(store.list_projects(include_archived=True)) == 1


def test_project_archive_does_not_stamp_its_kbs(store):
    """Cascade-by-read: the KB row keeps its own NULL so unarchive is lossless."""
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, archived=True)
    [proj] = store.list_projects(include_archived=True)
    assert proj["archived_at"] is not None
    assert proj["kbs"][0]["archived_at"] is None
    store.set_archived(project_id=pid, archived=False)
    [restored] = store.list_projects()
    assert len(restored["kbs"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_archive_sqlite.py -v`
Expected: the five Task 1 tests still pass; the five new ones FAIL with `KeyError: 'finding_count'` and `TypeError: list_projects() got an unexpected keyword argument 'include_archived'`

- [ ] **Step 3: Update the protocol signature**

In `delapan/store/base.py`, replace the `list_projects` stub (lines 149-157):

```python
    def list_projects(self, *, include_archived: bool = False) -> list[dict]:
        """All of the caller's projects with their KBs, for client discovery.

        Returns ``[{project, project_id, archived_at, kbs: [{kb, kb_id,
        finding_count, last_finding_at, archived_at}]}]``. ``finding_count`` and
        ``last_finding_at`` cover live findings only (``invalidated_at IS NULL``).

        ``include_archived`` is a single switch over both tiers: False (default)
        omits archived projects *and* archived KBs of active projects; True
        returns everything, each row carrying its ``archived_at`` so the caller
        can tell them apart. Cloud scopes to the authenticated user's org."""
        ...
```

- [ ] **Step 4: Implement on SQLiteStore**

Replace `list_projects` in `delapan/store/sqlite.py` (lines 638-666) entirely:

```python
    def list_projects(self, *, include_archived: bool = False) -> list[dict]:
        """Projects + KBs with live-finding activity, in one aggregate query."""
        rows = self._conn.execute(
            """
            SELECT p.id AS pid, p.name AS pname, p.archived_at AS parch,
                   k.id AS kid, k.name AS kname, k.archived_at AS karch,
                   COUNT(f.id) AS n, MAX(f.created_at) AS last
              FROM projects p
              LEFT JOIN kbs k
                ON k.project_id = p.id AND k.org_id = p.org_id
              LEFT JOIN findings f
                ON f.kb_id = k.id AND f.invalidated_at IS NULL
             WHERE p.org_id = ? AND p.name != ?
             GROUP BY p.id, k.id
             ORDER BY p.created_at, k.created_at;
            """,
            (_ORG, JOURNAL_SCOPE),
        ).fetchall()

        out: list[dict] = []
        by_pid: dict[str, dict] = {}
        for r in rows:
            if not include_archived and r["parch"] is not None:
                continue
            proj = by_pid.get(r["pid"])
            if proj is None:
                proj = {
                    "project": r["pname"],
                    "project_id": r["pid"],
                    "archived_at": r["parch"],
                    "kbs": [],
                }
                by_pid[r["pid"]] = proj
                out.append(proj)
            if r["kid"] is None:
                continue  # project with no KBs — LEFT JOIN filler row
            if not include_archived and r["karch"] is not None:
                continue
            proj["kbs"].append(
                {
                    "kb": r["kname"],
                    "kb_id": r["kid"],
                    "finding_count": int(r["n"]),
                    "last_finding_at": r["last"],
                    "archived_at": r["karch"],
                }
            )
        return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_archive_sqlite.py -v`
Expected: 10 passed

- [ ] **Step 6: Fix the fallout**

Run: `pytest -q`
Expected: failures in any test asserting the old shape. Update each to the new keys — do not reintroduce `snapshot_count`.

- [ ] **Step 7: Commit**

```bash
git add delapan/store/base.py delapan/store/sqlite.py tests/
git commit -m "feat(store): list_projects reports live-finding activity, single aggregate"
```

---

### Task 3: Supabase migration + `set_archived`

**Files:**
- Create: `migrations/2026-07-18-archive-lifecycle.sql`
- Modify: `delapan/store/supabase.py` (append after `list_projects`, line 100)
- Test: `tests/test_archive_supabase.py` (create)

**Interfaces:**
- Consumes: the `set_archived` contract from Task 1.
- Produces: identical `set_archived` on `SupabaseStore`, plus the `list_projects_with_activity` SQL function Task 4 calls.

- [ ] **Step 1: Write the migration**

Create `migrations/2026-07-18-archive-lifecycle.sql`:

```sql
-- Reversible KB lifecycle: NULL archived_at = active.
alter table projects add column if not exists archived_at timestamptz;
alter table kbs      add column if not exists archived_at timestamptz;

-- Single-aggregate replacement for the per-KB N+1 in list_projects.
-- PostgREST cannot express this aggregate, so it ships as an RPC — same
-- precedent as match_findings / match_kg_nodes.
create or replace function list_projects_with_activity(
  p_org_id uuid,
  p_include_archived boolean default false
)
returns table (
  project_id uuid,
  project_name text,
  project_archived_at timestamptz,
  kb_id uuid,
  kb_name text,
  kb_archived_at timestamptz,
  finding_count bigint,
  last_finding_at timestamptz
)
language sql
stable
security invoker
as $$
  select p.id, p.name, p.archived_at,
         k.id, k.name, k.archived_at,
         coalesce(f.n, 0), f.last
    from projects p
    left join kbs k
      on k.project_id = p.id and k.org_id = p.org_id
    left join lateral (
      select count(*) as n, max(created_at) as last
        from findings
       where findings.kb_id = k.id
         and findings.invalidated_at is null
    ) f on true
   where p.org_id = p_org_id
     and p.name <> '__journal__'
     and (p_include_archived
          or (p.archived_at is null
              and (k.id is null or k.archived_at is null)))
   order by p.created_at, k.created_at;
$$;
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_archive_supabase.py`:

```python
from __future__ import annotations

import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


def _seed(store):
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb("org1", pid, "main", create=True)
    return pid, kid


def test_archive_kb_sets_timestamp(monkeypatch):
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert out["kb_id"] == kid
    assert out["archived_at"] is not None
    assert out["finding_count"] == 0


def test_unarchive_clears_timestamp(monkeypatch):
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    assert out["archived_at"] is None


def test_archive_is_idempotent(monkeypatch):
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    a = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    b = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert a["archived_at"] == b["archived_at"]


def test_archive_unknown_raises(monkeypatch):
    store, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        store.set_archived(project_id="nope", archived=True)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_archive_supabase.py -v`
Expected: FAIL — `AttributeError: 'SupabaseStore' object has no attribute 'set_archived'`

- [ ] **Step 4: Implement on SupabaseStore**

In `delapan/store/supabase.py`, after `list_projects` (ends line 100):

```python
    def set_archived(
        self, *, project_id: str, kb_id: str | None = None, archived: bool
    ) -> dict:
        """Stamp/clear ``archived_at`` on a KB (or the project when kb_id is None)."""
        table, row_id = ("kbs", kb_id) if kb_id else ("projects", project_id)
        cur = (
            self._c.table(table).select("archived_at")
            .eq("id", row_id).eq("org_id", self._org_id)
            .limit(1).execute().data
        )
        if not cur:
            raise RuntimeError(f"{table} {row_id!r} not found")

        current = cur[0].get("archived_at")
        if archived and current is not None:
            stamp = current  # idempotent — don't move the timestamp
        else:
            stamp = _now_iso() if archived else None
            (
                self._c.table(table).update({"archived_at": stamp})
                .eq("id", row_id).eq("org_id", self._org_id).execute()
            )

        return {
            "project_id": project_id,
            "kb_id": kb_id,
            "archived_at": stamp,
            "finding_count": self._live_finding_count(project_id, kb_id),
        }

    def _live_finding_count(self, project_id: str, kb_id: str | None) -> int:
        """Live (non-invalidated) findings in one KB, or across a project's KBs."""
        if kb_id:
            kb_ids = [kb_id]
        else:
            kb_ids = [
                r["id"]
                for r in (
                    self._c.table("kbs").select("id")
                    .eq("org_id", self._org_id).eq("project_id", project_id)
                    .execute().data
                )
            ]
        if not kb_ids:
            return 0
        res = (
            self._c.table("findings").select("id", count="exact")
            .in_("kb_id", kb_ids).is_("invalidated_at", "null")
            .limit(1).execute()
        )
        return res.count or 0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_archive_supabase.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add migrations/2026-07-18-archive-lifecycle.sql delapan/store/supabase.py tests/test_archive_supabase.py
git commit -m "feat(store): archived_at migration + set_archived on Supabase tier"
```

---

### Task 4: Supabase `list_projects` rewrite

**Files:**
- Modify: `delapan/store/supabase.py:76-100` (replace `list_projects`)
- Modify: `tests/test_supabase_tenancy.py:36-43` (update the shape assertion)
- Test: `tests/test_archive_supabase.py` (extend)

**Interfaces:**
- Consumes: the `list_projects_with_activity` RPC from Task 3; the return shape from Task 2.
- Produces: `SupabaseStore.list_projects(*, include_archived=False)` matching Task 2's shape exactly.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_archive_supabase.py`:

```python
def _register_rpc(fake):
    """Stand in for list_projects_with_activity over the fake's tables."""
    def _fn(params):
        org, inc = params["p_org_id"], params["p_include_archived"]
        out = []
        for p in fake.tables.get("projects", []):
            if p["org_id"] != org or p["name"] == "__journal__":
                continue
            if not inc and p.get("archived_at") is not None:
                continue
            kbs = [k for k in fake.tables.get("kbs", [])
                   if k["project_id"] == p["id"] and k["org_id"] == org]
            if not kbs:
                out.append({"project_id": p["id"], "project_name": p["name"],
                            "project_archived_at": p.get("archived_at"),
                            "kb_id": None, "kb_name": None,
                            "kb_archived_at": None,
                            "finding_count": 0, "last_finding_at": None})
                continue
            for k in kbs:
                if not inc and k.get("archived_at") is not None:
                    continue
                live = [f for f in fake.tables.get("findings", [])
                        if f["kb_id"] == k["id"] and f.get("invalidated_at") is None]
                out.append({
                    "project_id": p["id"], "project_name": p["name"],
                    "project_archived_at": p.get("archived_at"),
                    "kb_id": k["id"], "kb_name": k["name"],
                    "kb_archived_at": k.get("archived_at"),
                    "finding_count": len(live),
                    "last_finding_at": max((f["created_at"] for f in live), default=None),
                })
        return out
    fake.register_rpc("list_projects_with_activity", _fn)


def test_list_projects_new_shape(monkeypatch):
    store, fake = make_store(monkeypatch)
    pid, kid = _seed(store)
    _register_rpc(fake)
    out = store.list_projects()
    assert out == [{
        "project": "repoA", "project_id": pid, "archived_at": None,
        "kbs": [{"kb": "main", "kb_id": kid, "finding_count": 0,
                 "last_finding_at": None, "archived_at": None}],
    }]


def test_archived_kb_hidden_by_default(monkeypatch):
    store, fake = make_store(monkeypatch)
    pid, kid = _seed(store)
    _register_rpc(fake)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    [proj] = store.list_projects()
    assert proj["kbs"] == []
    [proj_all] = store.list_projects(include_archived=True)
    assert proj_all["kbs"][0]["archived_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_archive_supabase.py -k new_shape -v`
Expected: FAIL — result still carries `snapshot_count` / `last_activity`

- [ ] **Step 3: Implement**

Replace `list_projects` in `delapan/store/supabase.py` (lines 76-100):

```python
    def list_projects(self, *, include_archived: bool = False) -> list[dict]:
        """Projects + KBs with live-finding activity, via one RPC round-trip."""
        rows = self._c.rpc(
            "list_projects_with_activity",
            {"p_org_id": self._org_id, "p_include_archived": include_archived},
        ).execute().data or []

        out: list[dict] = []
        by_pid: dict[str, dict] = {}
        for r in rows:
            pid = r["project_id"]
            proj = by_pid.get(pid)
            if proj is None:
                proj = {
                    "project": r["project_name"],
                    "project_id": pid,
                    "archived_at": r["project_archived_at"],
                    "kbs": [],
                }
                by_pid[pid] = proj
                out.append(proj)
            if r["kb_id"] is None:
                continue  # project with no KBs — LEFT JOIN filler row
            proj["kbs"].append(
                {
                    "kb": r["kb_name"],
                    "kb_id": r["kb_id"],
                    "finding_count": int(r["finding_count"]),
                    "last_finding_at": r["last_finding_at"],
                    "archived_at": r["kb_archived_at"],
                }
            )
        return out
```

- [ ] **Step 4: Update the stale shape assertion**

Replace `test_list_projects_shape` in `tests/test_supabase_tenancy.py` (lines 36-43):

```python
def test_list_projects_shape(monkeypatch):
    store, fake = make_store(monkeypatch)
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb("org1", pid, "main", create=True)
    fake.register_rpc("list_projects_with_activity", lambda _p: [{
        "project_id": pid, "project_name": "repoA", "project_archived_at": None,
        "kb_id": kid, "kb_name": "main", "kb_archived_at": None,
        "finding_count": 0, "last_finding_at": None,
    }])
    assert store.list_projects() == [{
        "project": "repoA", "project_id": pid, "archived_at": None,
        "kbs": [{"kb": "main", "kb_id": kid, "finding_count": 0,
                 "last_finding_at": None, "archived_at": None}],
    }]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_archive_supabase.py tests/test_supabase_tenancy.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add delapan/store/supabase.py tests/test_archive_supabase.py tests/test_supabase_tenancy.py
git commit -m "feat(store): Supabase list_projects via aggregate RPC, archive-aware"
```

---

### Task 5: Cross-backend parity suite

**Files:**
- Create: `tests/test_archive_parity.py`

**Interfaces:**
- Consumes: `set_archived` and `list_projects` on both backends (Tasks 1-4).
- Produces: nothing consumed downstream — this is the parity gate the vision invariant requires.

- [ ] **Step 1: Write the parity test**

Create `tests/test_archive_parity.py`:

```python
"""Both Store backends must return byte-identical shapes for the lifecycle API.

The vision invariant is that the engine cannot tell the two tiers apart. These
tests assert that directly: same calls, same keys, same values.
"""
from __future__ import annotations

import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase
from tests.test_archive_supabase import _register_rpc


@pytest.fixture()
def sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "parity.db"))
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("local", pid, "main", create=True)
    return s, pid, kid


@pytest.fixture()
def supabase_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    s = SupabaseStore("jwt", org_id="org1")
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("org1", pid, "main", create=True)
    _register_rpc(fake)
    return s, pid, kid


def _shape(store, pid, kid):
    """Run the lifecycle round-trip and return key sets + observable values."""
    archived = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    hidden = store.list_projects()
    shown = store.list_projects(include_archived=True)
    restored = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    after = store.list_projects()
    return {
        "archive_keys": sorted(archived),
        "archived_stamped": archived["archived_at"] is not None,
        "restore_stamped": restored["archived_at"] is None,
        "hidden_kbs": [k["kb"] for p in hidden for k in p["kbs"]],
        "shown_kbs": [k["kb"] for p in shown for k in p["kbs"]],
        "after_kbs": [k["kb"] for p in after for k in p["kbs"]],
        "project_keys": sorted(after[0]),
        "kb_keys": sorted(after[0]["kbs"][0]),
    }


def test_lifecycle_shapes_match_across_backends(sqlite_store, supabase_store):
    assert _shape(*sqlite_store) == _shape(*supabase_store)


def test_lifecycle_contract_is_correct(sqlite_store):
    """Pin the actual expected values, not just cross-backend agreement."""
    got = _shape(*sqlite_store)
    assert got["archive_keys"] == [
        "archived_at", "finding_count", "kb_id", "project_id"
    ]
    assert got["kb_keys"] == [
        "archived_at", "finding_count", "kb", "kb_id", "last_finding_at"
    ]
    assert got["project_keys"] == ["archived_at", "kbs", "project", "project_id"]
    assert got["archived_stamped"] is True
    assert got["restore_stamped"] is True
    assert got["hidden_kbs"] == []
    assert got["shown_kbs"] == ["main"]
    assert got["after_kbs"] == ["main"]
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_archive_parity.py -v`
Expected: 2 passed. A failure here means the two backends diverged — fix the backend, never the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/test_archive_parity.py
git commit -m "test(store): cross-backend parity gate for the lifecycle API"
```

---

### Task 6: HTTP routes

**Files:**
- Modify: `delapan/api/routes_projects.py` (whole file, currently 21 lines)
- Test: `tests/test_archive_routes.py` (create)

**Interfaces:**
- Consumes: `set_archived`, `list_projects` from Tasks 1-4.
- Produces: `GET /api/projects?include_archived=`, `PATCH /api/projects/{project}`, `PATCH /api/projects/{project}/kbs/{kb}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_archive_routes.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "routes.db"))
    from delapan.api.main import app
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    s.resolve_kb("local", pid, "main", create=True)
    return TestClient(app)


def test_get_projects_returns_new_shape(client):
    body = client.get("/api/projects").json()
    kb = body["projects"][0]["kbs"][0]
    assert "finding_count" in kb
    assert "snapshot_count" not in kb


def test_patch_kb_archives_and_hides(client):
    r = client.patch("/api/projects/repoA/kbs/main", json={"archived": True})
    assert r.status_code == 200
    assert r.json()["archived_at"] is not None
    assert client.get("/api/projects").json()["projects"][0]["kbs"] == []


def test_patch_project_archives(client):
    r = client.patch("/api/projects/repoA", json={"archived": True})
    assert r.status_code == 200
    assert client.get("/api/projects").json()["projects"] == []
    assert client.get("/api/projects?include_archived=true").json()["projects"]


def test_patch_unknown_project_404s(client):
    r = client.patch("/api/projects/nope", json={"archived": True})
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_archive_routes.py -v`
Expected: FAIL — 405 Method Not Allowed on the PATCH routes

- [ ] **Step 3: Implement**

Replace `delapan/api/routes_projects.py` entirely:

```python
"""Project/KB discovery + lifecycle routes — the HTTP mirror of the MCP tools.

    GET   /api/projects                     ──► store.list_projects()
    PATCH /api/projects/{project}           ──► store.set_archived(project)
    PATCH /api/projects/{project}/kbs/{kb}  ──► store.set_archived(project, kb)

The control panel's home screen reads the GET to populate its project/KB picker;
the row shape is whatever `Store.list_projects` returns, passed through.
Archiving is reversible and non-destructive — it stamps `archived_at` and
nothing else.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from delapan.mcp.tenancy import resolve_store

router = APIRouter(prefix="/api")


class ArchiveRequest(BaseModel):
    archived: bool


@router.get("/projects")
def list_projects(include_archived: bool = False) -> dict:
    return {"projects": resolve_store().list_projects(include_archived=include_archived)}


def _resolve_ids(store, project: str, kb: str | None) -> tuple[str, str | None]:
    """Names → ids, without creating anything. Raises 404 when absent."""
    try:
        org_id, project_id = store.resolve_project(project, create=False)
        kb_id = store.resolve_kb(org_id, project_id, kb, create=False) if kb else None
    except Exception as exc:  # noqa: BLE001 — missing project/KB is a 404, not a 500
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return project_id, kb_id


@router.patch("/projects/{project}")
def archive_project(project: str, body: ArchiveRequest) -> dict:
    store = resolve_store()
    project_id, _ = _resolve_ids(store, project, None)
    return store.set_archived(project_id=project_id, archived=body.archived)


@router.patch("/projects/{project}/kbs/{kb}")
def archive_kb(project: str, kb: str, body: ArchiveRequest) -> dict:
    store = resolve_store()
    project_id, kb_id = _resolve_ids(store, project, kb)
    return store.set_archived(project_id=project_id, kb_id=kb_id, archived=body.archived)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_archive_routes.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add delapan/api/routes_projects.py tests/test_archive_routes.py
git commit -m "feat(api): PATCH archive routes + include_archived on GET /projects"
```

---

### Task 7: MCP tool + explore auto-unarchive

**Files:**
- Modify: `delapan/mcp/server.py:1-15` (docstring), `:87-126` (explore), `:132-137` (projects), append new tool
- Test: `tests/test_archive_mcp.py` (create)

**Interfaces:**
- Consumes: `set_archived`, `list_projects` (Tasks 1-4).
- Produces: `delapan_archive(project, kb=None, archived=True) -> dict`; `delapan_projects(include_archived=False)`; `delapan_explore` result gains `"unarchived": bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_archive_mcp.py`:

```python
from __future__ import annotations

import pytest

from delapan.mcp import server


@pytest.fixture()
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "mcp.db"))
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("local", pid, "main", create=True)
    return s, pid, kid


async def test_archive_tool_round_trip(local):
    out = await server.delapan_archive("repoA", "main", archived=True)
    assert out["archived"] is True
    assert out["archived_at"] is not None
    assert out["finding_count"] == 0
    assert (await server.delapan_projects())["projects"][0]["kbs"] == []

    back = await server.delapan_archive("repoA", "main", archived=False)
    assert back["archived_at"] is None
    assert (await server.delapan_projects())["projects"][0]["kbs"]


async def test_archive_tool_unknown_kb_errors(local):
    out = await server.delapan_archive("repoA", "nope", archived=True)
    assert "error" in out


async def test_projects_include_archived(local):
    await server.delapan_archive("repoA", "main", archived=True)
    shown = await server.delapan_projects(include_archived=True)
    assert shown["projects"][0]["kbs"][0]["archived_at"] is not None


async def test_reads_still_work_on_archived_kb(local):
    """Archive hides from listings only — resume/search stay fully functional."""
    await server.delapan_archive("repoA", "main", archived=True)
    resumed = await server.delapan_resume("repoA", "main")
    assert "error" not in resumed
    assert "preamble" in resumed


def test_clear_archive_unarchives_kb_and_project(local):
    """The explore auto-unarchive helper, exercised without running a pipeline."""
    store, pid, kid = local
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    store.set_archived(project_id=pid, archived=True)

    ctx = SimpleNamespace(project_id=pid, kb_id=kid)
    assert server._clear_archive(store, ctx) is True

    [proj] = store.list_projects()
    assert proj["archived_at"] is None
    assert proj["kbs"][0]["archived_at"] is None
    # Second call is a no-op — nothing left to clear.
    assert server._clear_archive(store, ctx) is False
```

Add `from types import SimpleNamespace` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_archive_mcp.py -v`
Expected: FAIL — `AttributeError: module 'delapan.mcp.server' has no attribute 'delapan_archive'`

- [ ] **Step 3: Add the archive tool**

In `delapan/mcp/server.py`, after `delapan_projects` (line 137):

```python
@mcp.tool()
async def delapan_archive(project: str, kb: str | None = None, archived: bool = True) -> dict:
    """Archive or unarchive a project (omit ``kb``) or a single KB. Reversible and
    non-destructive — stamps ``archived_at`` and touches no finding, node, or edge.
    Archived KBs drop out of ``delapan_projects`` but stay fully readable by
    ``delapan_resume`` / ``delapan_search``; running ``delapan_explore`` against one
    unarchives it. Returns ``{"project", "kb", "archived", "archived_at",
    "finding_count"}`` — check ``finding_count`` to see what you just put away."""
    store = resolve_store()
    try:
        org_id, project_id = store.resolve_project(project, create=False)
        kb_id = store.resolve_kb(org_id, project_id, kb, create=False) if kb else None
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        target = f"{project}/{kb}" if kb else project
        return {"error": f"Not found ({target}): {exc}"}

    out = store.set_archived(project_id=project_id, kb_id=kb_id, archived=archived)
    return {
        "project": project,
        "kb": kb,
        "archived": archived,
        "archived_at": out["archived_at"],
        "finding_count": out["finding_count"],
    }
```

- [ ] **Step 4: Add `include_archived` to `delapan_projects`**

Replace lines 132-137:

```python
@mcp.tool()
async def delapan_projects(include_archived: bool = False) -> dict:
    """List the caller's projects (by name) with their KBs — for client discovery.
    Each KB carries ``finding_count`` and ``last_finding_at`` (live findings only).
    Archived projects/KBs are omitted unless ``include_archived`` is true.
    Returns ``{"projects": [...]}``."""
    store = resolve_store()
    return {"projects": store.list_projects(include_archived=include_archived)}
```

- [ ] **Step 5: Add explore auto-unarchive**

In `delapan_explore`, after `store = get_store(...)` (line 96), insert:

```python
    # Writing to a KB means it's live again. Either flag hides it, so clear both
    # the KB's and its project's, and report the flip in the result so the state
    # change is never silent.
    was_archived = _clear_archive(store, ctx)
```

And add the helper above `delapan_explore`:

```python
def _clear_archive(store, ctx) -> bool:
    """Unarchive ctx's KB and project if either was archived. Returns whether
    anything changed — explore reports it so the flip is visible, not silent."""
    project = next(
        (
            p
            for p in store.list_projects(include_archived=True)
            if p["project_id"] == ctx.project_id
        ),
        None,
    )
    if project is None:
        return False
    kb = next((k for k in project["kbs"] if k["kb_id"] == ctx.kb_id), None)
    was_archived = project["archived_at"] is not None or (
        kb is not None and kb["archived_at"] is not None
    )
    if was_archived:
        store.set_archived(project_id=ctx.project_id, kb_id=ctx.kb_id, archived=False)
        store.set_archived(project_id=ctx.project_id, archived=False)
    return was_archived
```

And change the return (line 126) to:

```python
    return {
        "exploration_id": exp_id,
        "finding_ids": ids,
        "count": len(ids),
        "unarchived": was_archived,
    }
```

- [ ] **Step 6: Update the module docstring**

In `delapan/mcp/server.py`, replace "four tools" (line 7) with "five tools" and add to the list after `delapan_projects`:

```
    delapan_archive   — archive/unarchive a project or KB (reversible)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_archive_mcp.py -v`
Expected: 5 passed

- [ ] **Step 8: Full suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add delapan/mcp/server.py tests/test_archive_mcp.py
git commit -m "feat(mcp): delapan_archive tool + explore auto-unarchive"
```

---

### Task 8: Update downstream consumers

**Files:**
- Modify: `skills/projects/SKILL.md` (in the plugin repo: `/Users/anthonysuherli/Repositories/8star/delapan-ai/skills/projects/SKILL.md`)
- Modify: `/Users/anthonysuherli/Repositories/8star/delapan-ai/frontend/src/api/client.ts` and any component reading `snapshot_count` / `last_activity`
- Modify: `README.md` (engine) if it documents the `list_projects` shape

**Interfaces:**
- Consumes: the final `list_projects` shape from Tasks 2 and 4, and `delapan_archive` from Task 7.
- Produces: nothing — this is the last task.

- [ ] **Step 1: Find every consumer**

```bash
grep -rn "snapshot_count\|last_activity" \
  /Users/anthonysuherli/Repositories/8star/delapan-ai \
  --include="*.ts" --include="*.tsx" --include="*.md" \
  --exclude-dir=node_modules --exclude-dir=backend
```

Expected: hits in `skills/projects/SKILL.md` and the frontend API layer. Fix every one.

- [ ] **Step 2: Update the projects skill**

In `skills/projects/SKILL.md`, replace the Workflow section body with:

```markdown
1. Call **`delapan_projects`** (no project/kb required). Pass
   `include_archived: true` to also see archived entries.
2. Present repos and branches using `finding_count` and `last_finding_at` —
   these count live findings only, so they reflect real activity.
3. If the user picks a target, note `project` + `kb` for subsequent
   `delapan_resume` / `delapan_search` calls.
4. To put a stale KB away, call **`delapan_archive`** with `project` (and `kb`
   for a single KB). It is reversible — `archived: false` restores it — and
   never deletes anything. Running `delapan_explore` against an archived KB
   unarchives it automatically.
```

- [ ] **Step 3: Update the frontend types**

In the frontend API layer, rename the KB fields on the project-listing type:

```ts
// before: snapshot_count: number; last_activity: string | null
finding_count: number;
last_finding_at: string | null;
archived_at: string | null;
```

Then fix every compile error the rename surfaces.

- [ ] **Step 4: Verify the frontend builds**

Run: `cd /Users/anthonysuherli/Repositories/8star/delapan-ai/frontend && npm run build`
Expected: clean build, no type errors

- [ ] **Step 5: Commit (two repos)**

```bash
cd /Users/anthonysuherli/projects/delapan
git add README.md && git commit -m "docs: list_projects reports finding activity, not snapshots"

cd /Users/anthonysuherli/Repositories/8star/delapan-ai
git add skills/projects/SKILL.md frontend/src
git commit -m "feat: consume finding_count/last_finding_at + delapan_archive"
```

---

## Deployment note

The Supabase migration must be applied **before** the new `list_projects` reaches
cloud, or the RPC call 404s. Apply `migrations/2026-07-18-archive-lifecycle.sql`
to the cloud project first, then deploy.

## Acceptance verification

After Task 8, confirm the two vision acceptance criteria directly:

```bash
pytest tests/test_archive_parity.py -v      # archive round-trips, both backends
```

Then, against the live store, confirm truthful activity — a KB written today must
report today's `last_finding_at`, which is the defect that started this work.
