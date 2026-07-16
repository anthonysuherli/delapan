# Gap-driven curation flywheel: persist coverage verdicts → ranked backlog → explicit consume

*2026-07-16 · Status: approved design, pre-implementation · Line: master · Portfolio item E4 from `delapan-ai/reports/2026-07-15-delapan-enhancement-brainstorm.md` · Adversarially verified against the codebase and the live cloud instance `gunqbyddzuwzpncfigro`; every verification finding folded in.*

**PROBLEM:** every coverage verdict the engine computes is thrown away, and the one seam built to keep it (`Store.record_access`) has zero call sites and is broken on the tier it was written for. **SOLUTION:** persist each verdict as an access event, aggregate gap/sparse queries into a recurrence-ranked backlog of curation topics, and let a promptless `delapan_explore` explicitly consume the top topic.

## Context / Problem

`select_preamble` computes a `rich`/`sparse`/`gap` verdict on every resume (`preamble.py:154-155`) and drops it on the floor the moment the tool returns. The KB therefore never learns what it was *asked* and failed to answer — the single highest-signal input to deciding what to explore next. `delapan_explore` requires a human to guess the topic.

`Store.record_access` was built for exactly this seam and is **dead code**: defs at `base.py:278`, `sqlite.py:1043`, `supabase.py:512`, plus one test (`tests/test_supabase_misc.py:43-50`). No engine call site exists (verified: `git grep record_access master` returns only defs, docs, and that test).

Worse, verified against the **live instance**, the cloud implementation has never worked:

| Claim | Live reality |
|---|---|
| `record_access` inserts `targets`, `created_at` (`supabase.py:517-519`) | Neither column exists. Live shape: `id bigint`, `org_id uuid`, `kb_id uuid`, `target_type text NOT NULL`, `target_id uuid`, `surface text NOT NULL`, `api_key_id uuid`, `query_text text`, `ts timestamptz NOT NULL default now()`. |
| The insert lands | `target_type` is `NOT NULL` and is never supplied → the insert would fail on shape even if it were permitted. |
| RLS permits the write | `access_events` has `relrowsecurity = true` with **one policy, `SELECT` only**. No `INSERT` policy ⇒ every user-JWT insert is rejected. No `DELETE` policy ⇒ pruning silently deletes nothing. |

Both failures are swallowed by `record_access`'s own never-raise contract, which is why the dead code looked alive. **Fixing `record_access` to the live schema is a component of this change, not an assumption of it.**

On SQLite, `access_events` does not exist at all — `record_access` is a literal `return None` (`sqlite.py:1043-1053`).

## Design

Two layers. Append-only **`access_events`** is ground truth (pruned on a horizon). **`curation_topics`** is a write-time-materialized backlog: near-duplicate queries collapse into one topic carrying a recurrence count.

Aggregation is **greedy online nearest-topic assignment** (exact-normalized-string fast path first, vector match second). The query embedding is **already computed on the hot path** (`preamble.py:146`, `server.py:124`), so clustering costs one vector search against a tiny per-KB table, in the background — **zero extra embed calls**. Exact-string grouping alone would miss paraphrases (the common case); a materialized table is required because near-duplicate clustering cannot be a cheap SQL view (vector aggregation over all events on every read), and it makes the backlog read O(1). Same idiom as KG node dedupe-by-cosine (`config.py:362`).

**Verdicts are computed on every resume, and are derivable for free on search.** `delapan_search` does *not* band today — it returns raw `match_findings` hits (`server.py:114-126`). It does not need `select_preamble` to gain a verdict: `band_findings` (`preamble.py:38`) and `assess_coverage` (`preamble.py:53`) are pure functions over rows the search already fetched. Search bands its own hits and records directly.

```
resume ─► select_preamble ─► (preamble, coverage)              ← unchanged hot path
              │ qvec, bands, verdict
              └─► schedule_record ──┐  (fire-and-forget, never blocks)
                                    │
search ─► embed ─► match_findings ─►│  band_findings ─► assess_coverage   (pure, zero IO)
              │ qvec, bands, verdict│
              └────────────────────►┤
                                    ▼
                        _record(kb, org, surface, query, coverage, bands, qvec)
                            ├─► store.record_access(+coverage,+band_counts) ─► access_events
                            ├─► gap/sparse ─► match_curation_topics ≥ threshold?
                            │       hit  → bump_curation_topic  (atomic recurrence+1,
                            │              last_seen, coverage, clear consumed/resolved)
                            │       miss → upsert_curation_topic (atomic INSERT … ON CONFLICT
                            │              (kb_id, query_norm) DO UPDATE recurrence+1)
                            ├─► rich ─► matching topic? → stamp resolved_at
                            └─► sampled prune: access_events older than retention

delapan_backlog / GET …/backlog ─► list_curation_topics ─► rank_backlog ─► ranked list
delapan_explore(prompt=None) ─► top open topic ─► stamp consumed_at ─► run_exploration
```

**Consumed/resolved semantics (settled).** The backlog shows `consumed_at IS NULL AND resolved_at IS NULL`. A recorded **rich** hit matching a topic stamps `resolved_at` (coverage flipped → done). A recorded **gap/sparse** hit clears both stamps and bumps recurrence — a consumed topic whose gap persists after explore self-heals back onto the backlog. A failed explore clears `consumed_at` immediately.

**Concurrency (decided).** There is an await boundary between `match_curation_topics` and the write, so two in-flight recordings of the same query — e.g. `delapan_resume` then `delapan_search` with the *same* text in one turn, both of which this design records — can interleave, both miss, and both insert. **Resolution: a unique index on `(kb_id, query_norm)`, conflict treated as a recurrence increment**, plus *all* increments done in SQL (`recurrence = recurrence + 1`), never read-modify-write in Python. Chosen over per-KB serialization because: (1) the verified concrete race is byte-identical text, which the index catches completely; (2) it is **cross-process** safe — the MCP server and the API server are separate processes writing the same cloud KB, where an in-process `asyncio.Lock` is useless; (3) no lock bookkeeping or per-KB lock leak; (4) in-SQL increment also kills the lost-update on the vector-hit path, which a lock around the insert alone would not. Residual paraphrase races produce a duplicate topic at recurrence 1 — benign and self-healing: the next vector match collapses onto whichever topic wins, and the loser decays out of the ranking and prunes.

## Components

### C1 · `delapan/core/curation/recorder.py` (new)

Fire-and-forget recording + topic transition logic — the **single** place transitions live (stores stay CRUD-dumb, so the logic is written once, not per tier). Mirrors the `_BG_TASKS` strong-ref pattern of `schedule_rebuild` (`synopsis.py:94-104`) and `schedule_kg_update` (`builder.py:257-267`) — CPython only weak-refs tasks, so an unreferenced `create_task` can vanish mid-flight.

```python
def schedule_record(store, *, kb_id, org_id, surface, query, coverage, bands, embedding) -> None
async def _record(...) -> None   # try/except everything; never raises
def normalize_query(q: str) -> str   # " ".join(q.lower().split())
```

`_record` wraps its whole body in try/except and logs at debug, matching `record_access`'s "must never raise" contract (`base.py:287`) and `maybe_rebuild_synopsis`'s best-effort shape (`synopsis.py:74-91`). Skips when `not cfg.enabled`, when `query` is absent, or when `len(query.strip()) < cfg.min_query_chars`.

### C2 · `delapan/core/curation/backlog.py` (new)

Pure ranking — no IO, unit-testable without a store:

```python
def rank_backlog(rows: list[dict], cfg: CurationConfig, now: datetime) -> list[dict]
# score = recurrence * coverage_weight * exp(-age_days / recency_half_life_days)
# coverage_weight = gap_weight | sparse_weight; age_days from last_seen
```

### C3 · `delapan/core/agent/preamble.py` (existing)

`select_preamble` (`preamble.py:131`) gains `surface: str | None = None, org_id: str | None = None`. When `surface` is set, curation is enabled, and a query exists, call `schedule_record` reusing the already-computed `qvec` (`preamble.py:146`) and `bands`/`coverage` (`preamble.py:154-155`). Verdict computation is untouched. Default `surface=None` means every existing caller is byte-identical until it opts in.

### C4 · `delapan/mcp/server.py` (existing)

- `delapan_resume` (`server.py:94`): pass `surface="resume", org_id=ctx.org_id`.
- `delapan_search` (`server.py:115`): band its own hits via the pure `band_findings`/`assess_coverage`, then `schedule_record(surface="search")`. Gated on `cfg.record_search`. **Search records: yes** — search queries are the strongest demand signal and the marginal cost is two pure functions over rows already in memory. **Guard:** skip when `limit < tiers.rich_hit_count` (default 3) — a `rich` verdict is arithmetically unreachable below that, so the verdict would be an artifact of the caller's `limit`, not of the KB. Default `limit` is 10 (`server.py:125`), so the guard is inert in practice.
- New tool `delapan_backlog(project, kb, limit=None) -> {"topics": [...]}` — ranked open topics.
- `delapan_explore` (`server.py:133`): `prompt: str | None = None`. Promptless path resolves with `resolve_tenant(create=False)` (never create a KB just to read an empty backlog) and returns a clean error dict on a missing KB, as `delapan_resume` does (`server.py:104-105`); ranks open topics; empty → `{"error": "backlog empty — pass a prompt, or run resume/search so gaps get recorded"}` with **no exploration row created**; else stamp `consumed_at`, use `query_text` as the focus prompt, proceed through the existing pipeline unchanged (`server.py:145-195`), and add `"backlog_topic"` to the result. The existing `except` block (`server.py:191`) additionally clears `consumed_at` so a failed run returns the topic to the backlog.

### C5 · `delapan/api/routes_findings.py` (existing)

- `GET /api/projects/{p}/kbs/{k}/backlog` alongside `resume` (`routes_findings.py:72`), for the sigma.js frontend.
- The `resume` route passes **`surface="resume", org_id=ctx.org_id`** — `ctx` is already in hand from `resolve_kb_or_404` (`routes_findings.py:79`, `deps.py:29`). Without this the frontend's `GET …/resume` (`getResume`, wrapper repo `delapan-ai/frontend/src/api/client.ts:211`) — the exact surface this design targets — would record nothing, invisibly.

**org_id resolution (stated explicitly).** Both mechanisms, and both are load-bearing for different reasons: **the caller passes `org_id`** (Protocol parity with `record_access`, whose `org_id` is a required kwarg), **and each store self-scopes on write** exactly as `insert_findings` already does on both tiers — `SupabaseStore` injects `self._org_id` (`supabase.py:517`, mandated by the cloud-tier spec §6.2) and `SQLiteStore` forces `_ORG = "local"` (`sqlite.py:42,282`). The store injection is what makes `org_id=None` structurally unable to reach a `NOT NULL` column from any caller, present or future; the parameter keeps the seam honest.

### C6 · `SupabaseStore.record_access` repair (`delapan/store/supabase.py:512-522`)

Rewrite the insert to the **live** schema: `{org_id: self._org_id, kb_id, target_type: "query", target_id: None, surface, query_text, coverage, band_counts, ts}`. Drops the nonexistent `targets`/`created_at`, supplies the `NOT NULL` `target_type`.

The `targets` kwarg stays on the signature (Protocol compatibility; `tests/test_supabase_misc.py:50` passes it) and is **not written** — E4 needs the query-level verdict row only. Per-target fan-out rows (`target_type='finding'`, one per served finding) can be added later with no schema change; the table and the rollup already model them.

**Verified compatible with the deployed rollup:** `rollup_access_events(p_day date)` groups by `(kb_id, org_id, target_type, target_id, surface, api_key_id)` into `access_rollup_daily`, whose conflict key `uq_access_rollup_daily` is **`NULLS NOT DISTINCT`** — so query-level rows with `target_id = NULL` aggregate and upsert idempotently rather than duplicating daily. The rollup selects explicit columns and never sees `coverage`/`band_counts`, so the new columns cannot perturb it.

### C7 · Docs + plugin surface

The four-tool surface is hardcoded in five places; `delapan_backlog` lands broken-in-docs without all of them:

- `delapan/mcp/server.py:6-12` — module docstring, "deliberately small — four tools".
- `README.md:24-26` (tool list) and `README.md:96` ("all 4 tools register and run").
- Wrapper `/Users/anthonysuherli/Repositories/8star/delapan-ai/.claude-plugin/plugin.json` + `marketplace.json` — both enumerate the four `/delapan:*` skills.
- Wrapper `skills/backlog/SKILL.md` (**new** — without it `delapan_backlog` ships with no slash command; the wrapper has only `explore/projects/resume/search`), plus a promptless-consume note in `skills/explore/SKILL.md`, which **already** calls the focus prompt "optional" — pre-existing drift this change finally makes true.

**Drift flagged:** CLAUDE.md names `delapan-ai/docs/technical-overview.md` as authoritative and house rules require updating its "Current state" in the same change. **That file does not exist** in either repo (verified by `find` across `8star/` and `Projects/delapan`). Nothing to update; README.md is the real doc surface. Flag the stale CLAUDE.md pointer; do not create the file to satisfy a rule.

**E5 touchpoint (collision warning):** the sibling E5 spec also adds MCP tools and edits `skills/explore/SKILL.md`. Both changes touch `server.py`'s docstring, the README tool list, the plugin manifests, and that one SKILL.md. Land order is free, second-lander rebases; neither owns the file.

## Store changes

Protocol (`store/base.py`). `record_access` gains `coverage: str | None = None, band_counts: dict | None = None` — defaulted kwargs, so `test_supabase_misc.py:43-50` still passes. Five new methods, **all `async`** (see below), all CRUD-dumb:

```python
async def match_curation_topics(self, kb_id, query_embedding, match_count, min_similarity) -> list[dict]
async def upsert_curation_topic(self, row: dict) -> str        # atomic INSERT…ON CONFLICT(kb_id, query_norm) → recurrence+1
async def bump_curation_topic(self, kb_id, topic_id, *, coverage, seen_at) -> None   # atomic recurrence+1 in SQL
async def update_curation_topic(self, kb_id, topic_id, **patch) -> None              # consumed_at / resolved_at stamps
async def list_curation_topics(self, kb_id, *, include_closed=False, limit=None) -> list[dict]
async def prune_access_events(self, kb_id, older_than_iso) -> None                   # best-effort
```

**Why all async.** `SupabaseStore` wraps `record_access`'s blocking postgrest insert in `asyncio.to_thread` precisely to honor the never-blocks contract (`supabase.py:512-522`). Sync methods called inside the async `_record` task would run blocking HTTP **on the event loop**, stalling whatever the loop does next — including flushing the current response — which would falsify the byte-identical-latency guarantee on the cloud tier. Every new Supabase method therefore goes through `asyncio.to_thread`. `SQLiteStore` implements them as `async def` doing the sqlite3 call inline, the established pattern for `match_findings`/`insert_findings` (`sqlite.py:200,260`).

### SQLite (`store/sqlite.py`)

`record_access` stops being a no-op (`sqlite.py:1043-1053`) and inserts; its docstring changes — access events are no longer only a cloud billing concern, they are the curation ground truth. Vector search mirrors the `vec_distance_cosine` join (`sqlite.py:241`); topic ids are `uuid4().hex` and embeddings go in via `serialize_float32`, matching `insert_findings`.

**Migration placement (verified).** `_ensure_schema` runs `executescript(_SCHEMA)` **before** the `_ADD_COLUMN_MIGRATIONS` loop (`sqlite.py:184-196`), so an index over a newly-`ALTER`ed column must live in `_ADD_COLUMN_MIGRATIONS`, never `_SCHEMA`. **That gotcha does not bite here:** `access_events` is absent from SQLite entirely today (verified — zero hits in `sqlite.py`), so both tables are brand-new `CREATE TABLE IF NOT EXISTS` in `_SCHEMA`, complete with their `coverage`/`band_counts` columns and their indexes. **No `_ADD_COLUMN_MIGRATIONS` entry is needed on this tier** — the ALTER is Supabase-only, because only Supabase has a pre-existing `access_events`.

Columns mirror the live cloud names (`target_type`/`target_id`/`ts`, not `targets`/`created_at`) for shape parity. The surrogate id diverges — cloud `bigint` identity, local `INTEGER` rowid — both opaque and never returned.

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

`upsert_curation_topic` is one statement — the race fix is the index, not a lock:

```sql
INSERT INTO curation_topics (id, org_id, kb_id, query_text, query_norm, coverage,
                             first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(kb_id, query_norm) DO UPDATE SET
  recurrence = recurrence + 1, last_seen = excluded.last_seen,
  coverage = excluded.coverage, consumed_at = NULL, resolved_at = NULL;
```

### Supabase — migration (lives outside this repo; not complete until applied)

```sql
-- 1. access_events: verdict columns (additive; rollup_access_events selects
--    explicit columns and is unaffected).
alter table access_events
  add column if not exists coverage text,
  add column if not exists band_counts jsonb;
create index if not exists idx_access_events_kb_ts on access_events (kb_id, ts);

-- 2. access_events RLS is SELECT-only today — writes and prunes are silently
--    rejected. Mirror the findings table's policies verbatim.
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

-- 4. RPCs. Param naming follows the deployed convention verified on the live
--    instance — match_findings(query_embedding, match_kb_id, match_count,
--    min_similarity) — NOT a p_-prefixed one.
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

Reads scope by `kb_id` only — the user JWT + RLS do org scoping (cloud-tier spec §6.3). Embeddings are sent as a bracketed `"[f1,…,f1536]"` string via the existing `self._vec` helper (`dim = 1536`, `config.py:343`; `config.yaml` pins 1536 for `google/gemini-embedding-001`).

## Config

`CurationConfig` in `core/config.py`, wired into `AppConfig` (`config.py:385`) + a `curation:` section in `config.yaml`. Every knob overridable as `DLP_CURATION__<FIELD>` (`config.py:407-408`).

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

`enabled: true` by default — unlike `UserProfileConfig`'s dark-launch `False` (`config.py:219`). Justified: recording is zero-latency, best-effort, and invisible; **nothing** changes until someone calls `delapan_backlog` or omits `prompt`. A dark-launched flywheel accumulates nothing and is useless on the day it is switched on.

**Prune trigger.** The repo has no scheduler (verified: no celery/apscheduler/cron dependency in `pyproject.toml`), so pruning is sampled inside the already-backgrounded `_record`: with probability `prune_sample_rate`, delete that KB's `access_events` older than `events_retention_days`. Stateless, no scheduler, no cross-process coordination; at ~100 recordings/KB it fires about once, and it is an indexed `DELETE` on `(kb_id, ts)`.

## Error handling

- **Recording failure of any kind** (store down, table missing pre-migration, RLS rejection): `_record` swallows everything and logs at debug — matching `record_access`'s "must never raise" contract (`base.py:287`). **Resume/search latency and results are byte-identical with curation on or off**; every store call in the task is `await`ed off the loop (`to_thread` on cloud), so the hot path never blocks on it.
- **Cloud migration not yet applied:** inserts fail inside the swallow; `delapan_backlog` returns an empty list. Degraded, not broken — and identical to today's behavior, since cloud `record_access` has never actually written a row.
- **Old SQLite DB:** the new tables appear via `CREATE TABLE IF NOT EXISTS` on next open (`_ensure_schema`, `sqlite.py:184`). No migration entry, no user action.
- **Promptless explore, empty/exhausted backlog:** informative error dict; tenant resolved with `create=False`, so no KB and no exploration row are created.
- **Explore failure after consumption:** the existing failed-row path (`server.py:192`) plus clearing `consumed_at` → the topic resurfaces on the backlog.
- **Embeddings unavailable:** the HTTP resume route already 503s before `select_preamble` when a query has no key (`routes_findings.py:80-81`), so no `qvec` and no recording exists on that path — consistent with coverage not being computed.
- **Prune failure:** inside the same swallow; events accumulate until the next sampled attempt.

## Testing & acceptance criteria

1. **`rank_backlog` (pure, no IO):** ordering by `recurrence × weight × decay`; `gap` outranks `sparse` at equal recurrence; a 2×half-life-old topic scores ¼ of an identical fresh one; empty input → empty.
2. **Recorder transitions (stub store, no IO):** gap→insert; paraphrase at sim ≥ threshold→`bump` (not a second row); rich→`resolved_at` stamped; gap-after-consume→both stamps cleared and recurrence bumped; `len(query) < min_query_chars`→no write; `enabled: false`→no write.
3. **`_record` never raises:** every store method monkeypatched to raise → `schedule_record` completes and `select_preamble` returns normally (mirrors `test_supabase_misc.py:43`).
4. **Race:** two `_record` coroutines for the **same** query on one KB run concurrently via `asyncio.gather` → exactly **one** row in `curation_topics` with `recurrence == 2`. Asserted on SQLite (real unique index) and on the fake-Supabase twin.
5. **SQLite round-trip** (`test_store_sqlite.py`): `record_access` writes a row (no longer a no-op); upsert/match/bump/list/update/prune parity against a fake-Supabase twin in the style of `tests/test_supabase_findings.py` — same method surface, same returned shape.
6. **Cloud shape (fake client, `test_supabase_misc.py`):** `record_access` issues an insert whose keys are **exactly** the live column set — asserts `target_type == "query"` present and `targets`/`created_at` **absent**. This is the regression test for the schema bug found here.
7. **Promptless explore** (`test_mcp_smoke.py` style): empty backlog → error dict, **no exploration row created**; seeded backlog → top topic consumed, `consumed_at` stamped, `backlog_topic` in the result; forced pipeline failure → `consumed_at` cleared and the topic is back on the backlog.
8. **Search parity:** `delapan_search` results are byte-identical with `record_search` true vs false; `limit < rich_hit_count` → no recording.
9. **E1 replay proof (when E1 lands):** replay each KB's golden query set through `select_preamble(surface="resume")`; `access_events.coverage` matches the golden verdict for every query; golden paraphrase pairs land in **one** topic at `topic_match_threshold` (cluster purity).
10. **Flywheel metric (the acceptance gate):** seed a KB, record a gap query set until a top topic exists → `delapan_explore(prompt=None)` consumes it → re-run **that topic's** golden queries → the verdict flips `gap`→`rich`/`sparse` **and** the recorded rich hit stamps `resolved_at`, so the topic leaves the backlog without human input.
11. **Live cloud gate (manual, post-migration):** with `DELAPAN_BACKEND=cloud`, one `delapan_resume` produces exactly one `access_events` row carrying `coverage`, readable back under the MCP user's JWT — proving the new INSERT policy and the repaired column shape. `select rollup_access_events(current_date)` still succeeds and is idempotent across two calls.

## Sequencing & dependencies

**This design is independent of E1/E2/E3 and buildable now.** Build order:

1. `CurationConfig` + `config.yaml` + Protocol signatures.
2. SQLite: `_SCHEMA` tables, `record_access`, the five new methods.
3. Supabase migration (SQL above) → **C6 `record_access` repair** → the five new methods.
4. `recorder.py` + `backlog.py` with unit tests (1–4).
5. Hooks: `select_preamble`, `delapan_resume`, `delapan_search`, the resume route.
6. Surfaces: `delapan_backlog`, promptless `delapan_explore`, `GET …/backlog`.
7. **C7 docs + plugin skills** — `server.py` docstring, README ×2, plugin manifests ×2, new `skills/backlog/`, `skills/explore/SKILL.md` note.

**Graceful degradations:**
- **E1 late** → bands stay miscalibrated for `gemini-embedding-001` (known: write-path spec §Problem), yielding more `gap` verdicts and a noisier backlog. Recurrence ranking still surfaces real demand, and the exact-string fast path still groups literal repeats with `topic_match_threshold` uncalibrated.
- **E2 late** → verdicts count rows E2 would later retire. Harmless: those findings exist today. When E2 lands, `match_findings` gains `AND invalidated_at IS NULL` (**per the approved write-path spec §C3**, both tiers) and verdicts — and therefore recordings — reflect live findings only, **with no change to this design**. E4 reads verdicts; it does not care how they were computed.
- **E3 late** → retrieval stays embedding-only, and the embedding this design reuses is the same one retrieval used, so there is no query-representation drift to reconcile.
- **Cloud migration late** → local tier fully functional; cloud degrades to today's silent no-op.

## Open risks

1. **Uncalibrated `topic_match_threshold` (0.83) for `google/gemini-embedding-001`** splits paraphrases or merges distinct gaps. *Resolution:* the unique index makes literal repeats exact regardless of the threshold, so the blast radius is paraphrases only; extend E1's band-calibration script to emit paraphrase-pair similarity stats and tune via `DLP_CURATION__TOPIC_MATCH_THRESHOLD` with no code change.
2. **Raw query text retained in cloud `access_events` and `curation_topics`** (multi-tenant, possibly sensitive). *Resolution:* membership-based RLS on both tables (the `access_events` INSERT/DELETE policies added here close a real hole — the table has been SELECT-only), plus the sampled 90-day prune; `events_retention_days` is per-deploy shortenable. `curation_topics` is deliberately **not** pruned on that horizon — a topic is the backlog item itself, and it exits by being resolved or consumed.
3. **A noise topic (typo, one-off) consumes an expensive promptless explore.** *Resolution:* `min_query_chars` filters the worst; explicit consume plus the `delapan_backlog` view keeps a human in the loop by construction (there is no auto-explore on threshold, by decision); add a min-recurrence consumption knob only if observed in practice.
