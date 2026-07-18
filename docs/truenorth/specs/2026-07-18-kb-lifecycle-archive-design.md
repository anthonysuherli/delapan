# Design: KB lifecycle — reversible archive + truthful activity

**Date:** 2026-07-18
**Status:** Approved, pending implementation plan
**Vision goals served:** *Reversible KB lifecycle*; *Two tiers at protocol parity*
(`docs/truenorth/vision.md`, amended 2026-07-18)

## Problem

Two defects, one surface.

**No lifecycle.** The store holds 29 projects and 83 KBs. Nothing in the codebase
can delete or archive a project or a KB — not the MCP server, not the HTTP API,
not the `Store` protocol. Deletion exists only at the leaf tier (`delete_finding`,
`delete_kg_node`, `delete_kg_edge`). Abandoned feature-branch KBs and structural
duplicates accumulate with no way to put them away.

**Untrustworthy activity reporting.** `list_projects` ranks every KB by
`snapshot_count` and `last_activity`, both derived from findings where
`category = 'snapshot'` (`delapan/store/supabase.py:91-98`,
`delapan/store/sqlite.py:654-661`). Nothing in delapan writes that category. It is
a **br8n** convention — `br8n_capture` → `persist_snapshot()` builds a finding with
`category: "snapshot"` (`br8n/capture/service.py:34-45`). br8n stopped writing to
this store on **2026-06-09**.

Measured state as of 2026-07-18:

| Metric | Value |
|---|---|
| Findings with `category='snapshot'` | 45, newest 2026-06-09 |
| All other findings | 3,321, newest 2026-07-18 |
| Distinct categories | 589 |

The result: every KB reports as dormant while 18 of them were written to in the
last seven days. The metric that would tell you what is safe to archive is the
one that is broken, so the two defects must be fixed together.

The vision names br8n an explicit Non-Goal, yet delapan's primary discovery
surface is keyed to a br8n-owned field.

## Non-Goals

- Hard deletion, purge, or GC. Archiving sets state; it never removes rows.
- Frontend UI for archiving (API + MCP only in this change).
- Bulk archive operations.
- Any auto-archive-by-staleness heuristic. The user decides what is stale.

## Design

### 1. Schema — one nullable timestamp per tier

Add `archived_at` to `projects` and `kbs` on both backends. Nullable timestamp,
not a boolean: it records *when*, supports "archived three months ago", and
matches the codebase's existing `invalidated_at` / `revoked_at` convention.

**SQLite** — append two entries to the `_ADDITIVE_MIGRATIONS` list
(`delapan/store/sqlite.py:135-150`), which already runs each `ALTER TABLE ... ADD
COLUMN` individually and swallows duplicate-column errors:

```sql
ALTER TABLE projects ADD COLUMN archived_at TEXT;
ALTER TABLE kbs ADD COLUMN archived_at TEXT;
```

**Postgres** — `migrations/2026-07-18-archive-lifecycle.sql`:

```sql
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at timestamptz;
ALTER TABLE kbs      ADD COLUMN IF NOT EXISTS archived_at timestamptz;
```

Existing rows get `NULL` — every current project and KB is active by default, so
the migration is a no-op behaviourally.

### 2. Store protocol

One new method and one changed signature in `delapan/store/base.py`. Both land on
`SQLiteStore` and `SupabaseStore` in the same change — required by the parity
invariant.

```python
def set_archived(self, *, project_id: str, kb_id: str | None = None,
                 archived: bool) -> dict:
    """Archive or unarchive a project (kb_id=None) or a single KB.

    Returns {"project_id", "kb_id", "archived_at", "finding_count"}.
    ``kb_id`` is None in the project-level response, and ``finding_count`` is
    then the sum of live findings across all the project's KBs.
    """

def list_projects(self, *, include_archived: bool = False) -> list[dict]: ...
```

Semantics:

- **Idempotent.** Archiving an already-archived target returns the existing
  `archived_at` unchanged rather than bumping the timestamp.
- **No create-on-demand.** Unlike `resolve_project` / `resolve_kb`, a missing
  project or KB is an error. Archiving must never conjure the thing it archives.
- **Non-destructive.** Sets a column. Touches no finding, node, edge, or
  `grounded_in` provenance.

### 3. Cascade by read, not by write

Archiving a project does **not** stamp `archived_at` on its KBs. A KB is
*effectively archived* when its own `archived_at` is set **or** its parent
project's is.

This keeps unarchive lossless: restoring a project returns each KB to exactly the
individual state it had before, with no fan-out writes to record and undo. It
also means the cascade cannot partially fail.

### 4. `list_projects` rewrite

Replace the snapshot-derived fields. `snapshot_count` and `last_activity` are
**removed**, not retained as deprecated. Keeping a field nothing writes is
precisely what the new invariant ("activity metrics derive from first-class engine
writes") forbids.

New per-KB shape:

```json
{ "kb": "dev", "kb_id": "...", "finding_count": 104,
  "last_finding_at": "2026-07-16T00:34:14Z", "archived_at": null }
```

Per-project shape gains `"archived_at"`.

`include_archived` is a single switch governing both tiers: when `False` (the
default) the result omits archived projects *and* archived KBs of active
projects; when `True` everything is returned, each carrying its `archived_at` so
the caller can tell them apart. There is no way to request one tier's archived
rows without the other's — the simpler contract is worth more than the
flexibility.

Counts cover **live findings only** — `invalidated_at IS NULL` — so the
bi-temporal supersede path does not inflate them.

**Fix the N+1 while here.** The current implementation issues one query per KB
(`supabase.py:84-96`), so a call at present scale costs 100+ round trips. Rewrite
as a single aggregate:

- SQLite: one `JOIN` + `GROUP BY` across `projects`, `kbs`, `findings`.
- Supabase: a `list_projects_with_activity()` RPC, following the existing
  `match_findings` / `match_kg_nodes` RPC precedent — PostgREST cannot express the
  aggregate directly.

**Downstream consumers requiring update in the same change:**
`skills/projects/SKILL.md` and `frontend/src/api/`.

### 5. HTTP API

Extend `delapan/api/routes_projects.py`, which currently holds only `GET
/projects`:

```
GET   /projects?include_archived=false
PATCH /projects/{project}            {"archived": bool}
PATCH /projects/{project}/kbs/{kb}   {"archived": bool}
```

`PATCH` returns the `set_archived` payload. Unknown project or KB → 404.

### 6. MCP surface

```python
delapan_archive(project: str, kb: str | None = None, archived: bool = True)
delapan_projects(include_archived: bool = False)
```

This makes `delapan_archive` the first state-mutating tool on a server that has
been read/append-only (`resume`, `search`, `explore`, `projects`). Two mitigations:

- The response echoes `finding_count`, so archiving reports what was put away — a
  surprising number is a signal to stop and unarchive.
- `archived` is an explicit parameter with no bulk form, so each call affects
  exactly one project or one KB.

### 7. Explore auto-unarchives

In the explore path, after `resolve_kb`, if the target is effectively archived,
clear its `archived_at` and include `"unarchived": true` in the result. Writing to
a KB means it is live again; requiring a manual unarchive first would add friction
to exactly the moment the user has decided the work matters.

The flip is **reported, never silent** — `delapan_explore` surfaces the flag so
the state change is visible in the tool result.

Only the KB's own flag is cleared. If the parent project is archived, explore
clears that too, since the KB would otherwise remain effectively archived.

## Error handling

| Case | Behaviour |
|---|---|
| Archive unknown project/KB | Error (HTTP 404 / MCP error). No create-on-demand. |
| Archive an already-archived target | No-op; returns existing `archived_at`. |
| Unarchive an active target | No-op; returns `archived_at: null`. |
| Explore into archived KB | Auto-unarchive, `"unarchived": true` in result. |
| `resume` / `search` on archived KB | Work normally. Archive hides from listings only. |

## Testing

A shared parity suite parameterised over both backends — `SQLiteStore` against a
temp DB (the existing `conftest.py` fixture) and `SupabaseStore` against
`tests/fake_supabase.py`. Identical assertions on both; a divergent return shape
fails the suite.

Cases:

1. **Round-trip** — archive a KB → absent from default `list_projects` → present
   with `include_archived=True` → unarchive → present again, `finding_count`
   unchanged. *(Vision acceptance criterion.)*
2. **Project cascade** — archiving a project hides it and all its KBs; unarchiving
   restores per-KB state exactly, including a KB archived individually beforehand.
3. **Idempotence** — double-archive does not move `archived_at`.
4. **No create-on-demand** — archiving an unknown project raises, and creates
   nothing.
5. **Explore auto-unarchive** — explore into an archived KB clears the flag and
   reports `"unarchived": true`.
6. **Truthful activity** — a KB with a finding written now reports
   `last_finding_at` of now, and a `finding_count` matching live findings.
7. **Bi-temporal exclusion** — a superseded finding does not count toward
   `finding_count`.

## Risks

- **Breaking response shape.** Removing `snapshot_count` / `last_activity` breaks
  `skills/projects/SKILL.md` and the frontend until updated in the same change.
  Accepted: the invariant forbids retaining the dead field.
- **First mutating MCP tool.** Mitigated by single-target calls and
  `finding_count` echo, but it does change the server's character.
- **Supabase RPC.** The aggregate needs a deployed function; the migration must
  ship with the code or `list_projects` breaks on cloud.

## Follow-up, explicitly deferred

Once truthful activity reporting exists, the original motivating question — which
of the 83 KBs are genuinely dormant — becomes answerable from data. Acting on that
answer is a separate task, not part of this change.
