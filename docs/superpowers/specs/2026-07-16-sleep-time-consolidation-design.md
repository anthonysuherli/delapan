# Sleep-time consolidation: whole-KB clustering, cross-run merge, conservative stale marking

*2026-07-16 · Status: approved design, pre-implementation · Line: master · Portfolio item E6 from `delapan-ai/reports/2026-07-15-delapan-enhancement-brainstorm.md` · Depends on the approved [write-path dedup spec](2026-07-16-write-path-dedup-design.md) (E1+E2) · Adversarially verified against the codebase; every finding folded in.*

PROBLEM: knowledge only ever accretes, and the one background pass that exists reads a truncated slice of it through a fragile parse.
SOLUTION: a debounced background consolidator that clusters the whole KB once and drives three passes off that one clustering — synopsis, cross-run merge, conservative stale marking.

## Problem

Three defects, all verified:

1. **The synopsis reads a truncated window through a fragile parse.** `maybe_rebuild_synopsis` asks for `store.list_findings(kb_id, limit=200)` (`delapan/core/agent/synopsis.py:85`), but **both tiers clamp to 100** — `n = min(limit or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)` with `LIST_MAX_LIMIT = 100` at `store/sqlite.py:84,375` and `store/supabase.py:23,162`. The window has silently been 100 most-recent findings, not 200, and never the whole KB. What survives is then parsed by a bracket slice — `json.loads(text[text.find("[") : text.rfind("]") + 1])` (`synopsis.py:62`) — whose failure mode is a logged warning and `[]` (`synopsis.py:66-68`).
2. **Cross-run duplicates accumulate.** E2 resolves each candidate at insert time against `neighbor_top_k=5` neighbors above `neighbor_min_similarity=0.6` (`config.py:385-386`). That is a *forward* guard with a top-k horizon: duplicates that were co-inserted in one batch, that sat below the floor at the time, or that predate E2 entirely, are never revisited. E2's C5 backfill is a one-shot manual script, not a standing process.
3. **Nothing ever ages.** After E2 lands, `invalidated_at` is written only by the explore path, only when a fresh candidate contradicts an existing row. A KB with no traffic never re-examines itself.

Post-E2 the substrate for fixing all three exists (bi-temporal columns, write primitives, a resolver, an events log). What is missing is a pass that uses them **across runs, over the whole KB, with no fresh candidate in hand**.

## Decisions (approved 2026-07-16)

1. **Trigger**: post-explore debounced fire-and-forget (the existing detached-task pattern), plus an on-demand `delapan_consolidate` MCP tool and `POST …/consolidate` API route.
2. **Scope**: (A) whole-KB topic-clustered synopsis with a structured parse, (B) cluster re-summarization driving E2 resolver ops, (C) conservative stale marking that **never auto-invalidates on age alone**.
3. **Framing** (corrected from the draft): the **query path stays read-only**; **cross-run merge and stale edits are owned by the consolidator**. E2's per-insert resolution (`core/memory/persist.py:83-146`) and the explore-path synopsis upsert (`mcp/server.py:121`, `api/routes_explore.py:93`) **remain where they are** — this spec does not centralize all memory edits, and an implementer must not remove either to satisfy a purity claim.
4. **Clustering is vector-native**: one Store call returns near-duplicate *pairs* computed by sqlite-vec / pgvector; Python does union-find over that pair list at two thresholds. No numpy/sklearn/scipy — none is a dependency (`pyproject.toml:7-21`, verified) and none is added.
5. **This spec owns `list_finding_digests`** — the enumeration method sibling spec E7 (lazy KG) also consumes. One method, this signature. No second `list_finding_titles`/`list_finding_ids` variant.

## Architecture

```
explore persist ──► schedule_consolidation(ctx, store)  (debounced, detached)  ─┐
delapan_consolidate / POST …/consolidate (force=?)  (awaited)  ─────────────────┤
                                                                                ▼
                                              run_consolidation(ctx, store, cfg)
                                                        │
             store.finding_near_pairs(kb, topic_min_sim)  ── ONE vector-native call
                                                        │
                          union-find over the pair list, pure Python, two thresholds
                        ┌───────────────────────────────┴───────────────────────┐
             topic clusters (sim ≥ 0.50)                     merge clusters (sim ≥ 0.80)
                        │                                     star-reduced around an anchor
                        ▼                                                        │
          A) build_synopsis                                                      ▼
             list_finding_digests → per-cluster titles           B) resolve(…, neighbor_sets=[anchor])
             ONE structured_completion → SynopsisEntries             NOOP → corroborate anchor + retire member
             → upsert_synopsis  (no bracket-slice parse)              UPDATE → member survives, anchor retires
                                                                      ADD  → leave untouched
                                                                      SUPERSEDE → NOT applied here ──┐
                                                                                                     ▼
                                                                             C) stale gate (default: never fires)
                                                                                → STALE_FLAG event only
                        └──────────► consolidations row + resolution_events(source='consolidation')
```

Retrieval is untouched. `match_findings` keeps E2 §C3's `AND invalidated_at IS NULL`; nothing in this spec is on a query path.

**Why one pair call, not per-finding KNN.** The vec0 self-join was live-tested against sqlite-vec v0.1.9 in this repo's venv and works: joining `vec_findings` to itself on `a.finding_id < b.finding_id` with `vec_distance_cosine(a.embedding, b.embedding)` returns the pair list in one statement. It is a brute-force O(n²) scan in C — the same shape as today's `match_findings`, which also full-scans with `vec_distance_cosine` and `ORDER BY dist LIMIT` rather than using a `MATCH` KNN constraint (`sqlite.py:239-247`). At the `max_findings_for_pairs = 5000` guard that is ≤12.5M distance computations in C, once, in the background. Cloud does it as one pgvector RPC.

## Components

### C1 · Store: `finding_near_pairs` + `list_finding_digests` (both tiers)

`delapan/store/base.py`, `store/sqlite.py`, `store/supabase.py`. See §Store changes for SQL. `list_finding_digests` is **the shared method named in Decision 5** — E7 consumes it for ids only.

### C2 · The pass — `delapan/core/memory/consolidate.py` (new)

```python
async def run_consolidation(ctx: TenantContext, store: Store, cfg: AppConfig, *,
                            trigger: str, force: bool = False) -> dict
def schedule_consolidation(ctx: TenantContext, *, store: Store | None = None) -> None
def clusters_from_pairs(pairs: list[dict], min_sim: float) -> list[list[str]]   # union-find, pure
def star_members(cluster: list[str], pairs: list[dict], anchor: str, cfg) -> list[tuple[str, float]]
def should_consolidate(live_count: int, last: dict | None, cfg: ConsolidationConfig) -> bool
```

- `schedule_consolidation` mirrors `schedule_kg_update` (`core/knowledge_graph/builder.py:257-267`) and `schedule_rebuild` (`synopsis.py:97-104`) exactly: `asyncio.create_task` + a module-level `_BG_TASKS` strong-ref set + `add_done_callback(discard)`, because CPython's loop only weak-refs tasks.
- `should_consolidate` mirrors `should_rebuild` (`synopsis.py:24-37`): false when `live_count <= 0`; true when no prior row; true when `live_count - last["finding_count_at_run"] >= debounce_delta`; true when `started_at` is older than `debounce_max_age_hours`. `force=True` bypasses it; `cfg.consolidation.enabled = False` short-circuits before any Store call.
- **Star reduction** (this is also Risk 1's mitigation): a merge cluster's **anchor** is its highest-`confidence` member, tie-broken oldest `created_at`, then lexically by id — all three available from `list_finding_digests`. `star_members` keeps only members with a **direct pair edge to the anchor** in the pair list, sorted by descending edge similarity, truncated to `max_cluster_members`. Members that joined the cluster only transitively (single-link chaining) are **left untouched this pass** — they carry no measured similarity to the anchor, so the resolver would be shown a number we do not have. If they are true duplicates they will anchor their own cluster on a later pass.
- Lifecycle: `create_consolidation` → run → `update_consolidation(status=…, ops=…, clusters_reviewed=…)`, mirroring the exploration row lifecycle (`base.py:104-115`).

### C3 · Phase A — whole-KB clustered synopsis (`core/agent/synopsis.py`, edit)

Replace `_build` / `_build_prompt` (`synopsis.py:40-68`) with:

```python
class SynopsisEntry(BaseModel):   topic: str; gloss: str
class SynopsisEntries(BaseModel): entries: list[SynopsisEntry] = Field(default_factory=list)

async def build_synopsis(store: Store, kb_id: str, topic_clusters: list[list[str]],
                         digests: dict[str, dict], cfg: AppConfig) -> list[dict]
```

- Input is `list_finding_digests(kb_id, limit=max_findings_for_pairs)` — the **whole KB**, not `list_findings(limit=200)`-clamped-to-100. This is defect 1's fix, and it deletes the truncation rather than raising the cap.
- Clusters are ordered by member count; each contributes up to `titles_per_cluster` titles sampled by descending confidence. Unclustered singletons are pooled into one trailing "misc" block.
- **One** `structured_completion(model=cfg.consolidation.synopsis_model, response_format=SynopsisEntries, fallback_model=cfg.consolidation.synopsis_fallback_model, use_json_schema=True)` (`core/clients/ai_gateway.py:63-74`). `SynopsisEntries` is fully typed, so strict `json_schema` mode is correct per that function's own contract (`ai_gateway.py:81-85`). The bracket-slice parse at `synopsis.py:62` is deleted.
- **`SynopsisConfig.model` cannot be reused — verified bug.** It defaults to `"claude-haiku-4-5"` (`config.py:205`, `config.yaml:46`), an **Anthropic-client** model name fed to `chat_model` → `ChatAnthropic` (`synopsis.py:17,58`; `core/clients/anthropic.py:13`). `structured_completion` routes through the AI Gateway, whose slugs are `vendor/model` with **dots** for versions (`MemoryConfig`: `"anthropic/claude-sonnet-4.6"`, `config.py:382`, and its docstring "Models are AI Gateway slugs (dots for versions)", `config.py:379`). Passing `SynopsisConfig.model` to `structured_completion` would fail on every call. Hence the new `consolidation.synopsis_model` / `synopsis_fallback_model` knobs. `SynopsisConfig.model` is **left alone** — nothing else in this change reads it, and `upsert_synopsis(model=…)` keeps recording whichever model actually ran.
- **max_entries scaling rule**: `effective = min(max(cfg.synopsis.max_entries, n_big), cfg.consolidation.entries_cap)` where `n_big` = topic clusters with ≥3 members. `synopsis.max_entries = 6` (`config.py:208`) stays the floor; `entries_cap = 12` the ceiling. A 12-topic KB stops being crushed into 6 lines; a 3-topic KB is not padded to 12.
- `maybe_rebuild_synopsis` keeps its trigger logic and its `except Exception` blanket (`synopsis.py:90-91`) and both explore call sites keep awaiting it (Decision 3). When called from the consolidator it is handed the clustering it already computed; when called from the explore path with no clustering it computes its own via `finding_near_pairs`. Signature parity is preserved for cloud callers.

### C4 · Phase B — cluster merge via `resolve()` with injected neighbors

**Resolver edit** (`core/memory/resolver.py`) — chosen mechanism, option (a):

```python
async def resolve(store, kb_id, candidates, embeddings, cfg, *,
                  neighbor_sets: list[list[dict]] | None = None) -> list[ResolutionDecision]
```

`neighbor_sets=None` is today's behavior byte-for-byte: the `for emb in embeddings` loop self-fetches via `store.match_findings(...)` (`resolver.py:71-82`). When `neighbor_sets` is given (length must equal `candidates`), that loop is **skipped** and `embeddings` is ignored — Phase B passes `[]`. Nothing else in `resolve()` touches `embeddings` (verified: `resolver.py:54-132`), so this is ~8 lines and no behavior change for the explore path. Everything downstream is reused verbatim, including the hallucinated-target demotion at `resolver.py:121-130`, which now naturally constrains targets to the anchor.

**Why not option (b)** (a Store method returning stored embeddings, letting `resolve()` self-fetch): it defeats the point. Self-fetched neighbors are arbitrary KB-wide `match_findings` hits, **not** the cluster we deliberately computed — we would cluster and then throw the clustering away. It also adds a vector-read method to both tiers that nothing else wants, and the cloud tier actively resists it: the deployed `match_findings` RPC returns a trimmed projection with no `embedding`, and the SupabaseStore spec states callers must not expect it back ([supabase-store spec](2026-06-22-supabase-store-cloud-tier-design.md) §6.4). E2's C5 backfill documents the same wall ("the Store exposes no vector read") and pays for it by **re-embedding every candidate**. Option (a) costs one keyword-only param, adds zero Store surface, and — because Phase B never inserts a row (below) — needs **zero embedding calls**.

**Application** — Phase B is E2 §C5's backfill applier, scoped to a cluster instead of an oldest-first whole-KB replay. Both share the defining property: *the candidate already exists as a live row*. Per member `m` (candidate) against anchor `a` (the sole injected neighbor, carrying its real body from `get_finding`):

| Decision | Applied |
|---|---|
| `ADD` | nothing. `m` stays live, untouched. (No insert — `m` is already a row.) |
| `NOOP(a)` | corroborate `a` per E2 §C2: `update_finding(a, provenance=_merge_provenance(a, m), confidence=max(a.confidence, confidence_from_sources(distinct urls)), content=None, embedding=None, title=None)` — E2 §C2's **None→keep** semantics — then `invalidate_finding(m, superseded_by=a)`. |
| `UPDATE(a)` | `m` (the refinement) survives, mirroring E2 §C5: union `a`'s provenance into `m` via `update_finding` with `confidence=confidence_from_sources(distinct urls of the union)`, then `invalidate_finding(a, superseded_by=m)`. **The anchor for the rest of the cluster becomes `m`.** |
| `SUPERSEDE(a)` | **not applied here** — routed to Phase C (§C5). |

- **`supersede_finding` is never called by Phase B.** That primitive inserts a new row (E2 §C2 Notes); both rows already exist here, so `update_finding` + `invalidate_finding` is the correct pair. This is why Phase B needs no embeddings and why `ResolutionOp` is consumed but never extended.
- Every op is lossless over the live set: no provenance URL leaves a live row, and every retired row carries a live `superseded_by` successor. E2's `get_finding` returns invalidated rows (§C2 Notes), so `kg_nodes.grounded_in` stays resolvable.
- **Prerequisite (master-side, not E2's).** Phase B feeds the resolver anchor bodies via `get_finding`. Master already round-trips string content — `"content": _json_load(r["content"], r["content"])` at `store/sqlite.py:71` (`_finding_from_row`) and again in `match_findings` at `sqlite.py:257`, both with the comment "never drop a non-JSON body to `{}`". **Gate Phase B on this master behavior, not on `fix/finding-content-roundtrip`** — E2's §C1 is explicit that the branch is redundant, conflicts, and must not be merged. `resolver.py:10-12`'s docstring claiming neighbor bodies are unreliable is stale; E2 §C1 removes it and extends `_candidate_block` (`resolver.py:43-51`) to carry neighbor bodies. Phase B is the direct beneficiary.
- **LLM budget**: one call per merge cluster. `max_cluster_members = 12` ≤ `memory.max_candidates_per_pass = 25` (`config.py:387`), so `resolve()`'s chunk loop (`resolver.py:95-97`) emits exactly one call, and every star member has exactly one neighbor so none short-circuits out of the LLM path (`resolver.py:89-91`). Whole pass: **≤ 1 + max_clusters_per_pass** calls (1 synopsis + ≤8 clusters = ≤9).

### C5 · Phase C — conservative stale marking (locked)

`invalidated_at` is set by Phase C **only when both** hold:

1. the resolver returned `SUPERSEDE` naming target `t` this pass, **and**
2. `stale_age_days > 0` **and** `t`'s last-touch is older than that threshold — newest `accessed_at` across `t`'s provenance entries, falling back to `created_at`. (`accessed_at` is real: `normalize_provenance` stamps it on every entry, `core/exploration/render.py:50,57`.)

**`stale_age_days = 0` by default → clause 2 is never true → Phase C invalidates nothing out of the box.** Age alone is never sufficient at *any* setting — clause 1 is unconditional. Every contradiction instead logs `op='STALE_FLAG'`, `source='consolidation'`, `target_finding_id=t`, `details={contradicted_by, last_touch}`.

**Why the consolidator won't act on contradiction the way the explore path does.** On the explore path a `SUPERSEDE` is backed by a *freshly crawled source* — the candidate is new evidence, so retiring the old row is justified (E2 §C2). In the consolidator both rows are old and neither has new evidence behind it; the resolver is guessing which of two stale claims is wrong. That is a signal to surface, not a verdict to apply. The age co-condition exists so an operator who *does* want automatic retirement must state how stale "stale" is — and even then, only contradicted rows retire.

When it does fire: `invalidate_finding(kb_id, t, superseded_by=<contradicting member id>)`. Never `supersede_finding` — no new row exists to point at.

### C6 · Triggers: MCP tool, API route, scheduler

- `delapan/mcp/server.py`: new `delapan_consolidate(project: str, kb: str, force: bool = False) -> dict` tool (awaited, returns the ops summary); add `schedule_consolidation(ctx, store=store)` immediately after `schedule_kg_update(ctx, ids, store=store)` at `server.py:121`. Same insertion after `routes_explore.py:94`.
- `delapan/api/routes_consolidate.py` (new): `POST /consolidate` on `APIRouter(prefix="/api/projects/{project}/kbs/{kb}")` — the prefix pattern at `routes_explore.py:38`. Plain JSON, no SSE. Registered in `api/main.py` alongside the other five routers (`main.py:38-42`).
- **Shared touchpoint — flag for E4/E5.** `mcp/server.py`'s module docstring hardcodes *"The surface is deliberately small — four tools"* and enumerates them (`server.py:6-12`). E6 adds `delapan_consolidate`; E4 adds a backlog tool; E5 adds job-style deepen tools. All three edit the same six lines. Whoever lands first replaces the count with the list; the others append their line. E5 also adds an API route — no conflict (separate module, separate `include_router` line).

### C7 · `resolution_events.source` — both tiers

The draft added this cloud-only; it must land on both. Model, SQLite, and Supabase all change — see §Store changes. `ResolutionEvent` (`core/memory/models.py:41-47`) gains `source: str = "insert"`, so E2's existing `persist.py` call sites (`persist.py:85,88,101,113,134`) keep working unchanged and keep writing `'insert'` by default. The consolidator passes `source="consolidation"` explicitly.

## Store changes

### Protocol (`delapan/store/base.py`)

```python
def list_finding_digests(self, kb_id: str, *, limit: int = 5000,
                         include_invalidated: bool = False) -> list[dict]:
    """Enumerate findings for whole-KB passes: id, title, category, confidence, created_at.

    Distinct from `list_findings`, which is a UI list view capped at 100
    (LIST_MAX_LIMIT) and unsuitable for whole-KB work. No content/provenance.
    Live-only unless `include_invalidated`. Ordered by created_at DESC."""

async def finding_near_pairs(self, kb_id: str, min_similarity: float, *,
                             limit: int = 20000) -> list[dict]:
    """Near-duplicate pairs within `kb_id`, computed backend-side.

    Returns [{"a": id, "b": id, "similarity": float}] with a < b lexically,
    similarity DESC, live rows only. Never returns self-pairs."""

def create_consolidation(self, org_id: str, kb_id: str, trigger: str) -> str: ...
def update_consolidation(self, consolidation_id: str, **patch) -> None: ...
def get_last_consolidation(self, kb_id: str, status: str | None = None) -> dict | None: ...
```

Reused from E2, unchanged: `update_finding` (None→keep), `invalidate_finding`, `insert_resolution_events`, `get_finding`, live-filtered `match_findings`, live-only `count_findings`.

### SQLite (`delapan/store/sqlite.py`)

**Schema-ordering trap.** `_ensure_schema` runs `executescript(_SCHEMA)` **first**, then loops `_ADD_COLUMN_MIGRATIONS` in try/except (`sqlite.py:192-204`). A new `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` is therefore fine; **any index over a newly-ALTERed column must go in `_ADD_COLUMN_MIGRATIONS`, never `_SCHEMA`**, or it runs before its column exists. E6 adds no index over `resolution_events.source` for exactly this reason — if one is ever wanted, it goes in the migrations list after the ALTER.

New in `_SCHEMA` (new table — safe):

```sql
CREATE TABLE IF NOT EXISTS consolidations (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL,
  trigger TEXT, status TEXT, finding_count_at_run INTEGER,
  clusters_reviewed INTEGER, ops TEXT, error TEXT,
  started_at TEXT NOT NULL, completed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_consolidations_kb ON consolidations(kb_id, started_at);
```

`resolution_events`'s `_SCHEMA` CREATE TABLE (`sqlite.py:119-122`) gains `source TEXT` — for fresh DBs — **and** a migration entry for DBs created before E6, mirroring exactly how E2 §C3 handles `new_finding_id`/`details`:

```python
_ADD_COLUMN_MIGRATIONS += [
    # 00NN: consolidation — provenance of a resolution event ('insert' | 'consolidation' | 'backfill')
    "ALTER TABLE resolution_events ADD COLUMN source TEXT;",
]
```

`insert_resolution_events`'s fixed column list (`sqlite.py:1095-1097`) gains `source`, bound from `e.get("source") or "insert"`; `list_resolution_events`'s SELECT (`sqlite.py:1118-1119`) and its row map gain it too. `finding_near_pairs` — one statement, live-tested against sqlite-vec v0.1.9:

```sql
SELECT va.finding_id AS a, vb.finding_id AS b,
       1 - vec_distance_cosine(va.embedding, vb.embedding) AS similarity
FROM vec_findings va
JOIN findings   fa ON fa.id = va.finding_id
JOIN vec_findings vb ON vb.finding_id > va.finding_id
JOIN findings   fb ON fb.id = vb.finding_id
WHERE fa.kb_id = ? AND fb.kb_id = ?
  AND fa.invalidated_at IS NULL AND fb.invalidated_at IS NULL
  AND 1 - vec_distance_cosine(va.embedding, vb.embedding) >= ?
ORDER BY similarity DESC LIMIT ?;
```

`list_finding_digests` reuses `_FINDING_LIST_COLS` (`sqlite.py:62`) minus `tags`, `WHERE kb_id = ? AND invalidated_at IS NULL`, `ORDER BY created_at DESC LIMIT ?` — **not** routed through `list_findings`, so it does not inherit `LIST_MAX_LIMIT`.

### Supabase (migration SQL — applied to the cloud project, outside this repo)

Ids are `uuid`. RLS is membership-based; the predicate is written out rather than referenced.

```sql
-- 1. consolidation lifecycle
create table if not exists consolidations (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  kb_id uuid not null references kbs(id) on delete cascade,
  trigger text not null check (trigger in ('post_explore','manual')),
  status text not null default 'running' check (status in ('running','completed','failed')),
  finding_count_at_run int,
  clusters_reviewed int default 0,
  ops jsonb not null default '{}'::jsonb,
  error text,
  started_at timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists idx_consolidations_kb on consolidations (kb_id, started_at desc);
alter table consolidations enable row level security;

create policy consolidations_select on consolidations for select
  using (org_id in (select org_id from org_members where user_id = auth.uid()));
create policy consolidations_insert on consolidations for insert
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));
create policy consolidations_update on consolidations for update
  using      (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

-- 2. resolution_events.source. MUST run AFTER E2's migration, which CREATEs this
--    table (see Sequencing). `add column if not exists` does NOT guard table
--    existence — applied first, this statement errors.
alter table resolution_events add column if not exists source text not null default 'insert';

-- 3. near-duplicate pairs, pgvector-native
create or replace function finding_near_pairs(
  p_kb_id uuid, p_min_sim float8, p_limit int default 20000)
returns table (a uuid, b uuid, similarity float8)
language sql stable security invoker as $$
  select f1.id, f2.id, 1 - (f1.embedding <=> f2.embedding)
  from findings f1
  join findings f2 on f2.kb_id = f1.kb_id and f2.id > f1.id
  where f1.kb_id = p_kb_id
    and f1.invalidated_at is null and f2.invalidated_at is null
    and 1 - (f1.embedding <=> f2.embedding) >= p_min_sim
  order by 3 desc
  limit p_limit;
$$;
```

`security invoker` is load-bearing: the function must run under the caller's JWT so the `findings` RLS policy scopes it. The `invalidated_at` predicates depend on E2 §C3's migration (§Sequencing).

`SupabaseStore` additions (`store/supabase.py`): `list_finding_digests` (PostgREST select, `.is_("invalidated_at", "null")`), `finding_near_pairs` (`.rpc("finding_near_pairs", {...})` under `asyncio.to_thread`, per the supabase-store spec's async rule), and the three consolidation lifecycle methods (`.table("consolidations")` CRUD with `org_id` injected on insert — `org_id` is NOT NULL and reads never filter on it, RLS does; supabase-store spec §6.2-6.3).

## Config

New `ConsolidationConfig` in `core/config.py`, registered on `AppConfig` (`config.py:400-416`), mirrored into `config.yaml`. Every knob is `DLP_CONSOLIDATION__<FIELD>`-overridable via the generic loader (`config.py:445-458`); nothing is hardcoded.

```yaml
consolidation:
  enabled: true                 # kill-switch, independent of memory.enabled
  debounce_delta: 25            # new live findings since last run => consolidate
  debounce_max_age_hours: 336   # 14d — a quiet KB still gets one pass
  topic_cluster_min_sim: 0.50   # loose: Phase A grouping
  merge_cluster_min_sim: 0.80   # tight: Phase B. Matches exploration.fuzzy_match_threshold (config.py:281)
  max_findings_for_pairs: 5000  # O(n^2) guard: skip the pass above this, log, mark completed
  max_pairs: 20000              # LIMIT on finding_near_pairs
  max_clusters_per_pass: 8      # LLM budget: <= 1 + this call per pass
  max_cluster_members: 12       # <= memory.max_candidates_per_pass (25) => one call per cluster
  titles_per_cluster: 12        # Phase A prompt budget
  entries_cap: 12               # synopsis max_entries ceiling (floor stays synopsis.max_entries=6)
  stale_age_days: 0             # 0 => Phase C never invalidates. Age alone NEVER invalidates at any value.
  running_ttl_minutes: 30       # a `running` row younger than this blocks a concurrent pass
  synopsis_model: anthropic/claude-haiku-4.5      # AI Gateway slug (dots) — NOT synopsis.model
  synopsis_fallback_model: openai/gpt-5.4-mini
```

## Error handling

- The whole pass is wrapped like `maybe_rebuild_synopsis` (`synopsis.py:90-91`): it **never raises into the trigger path**. The consolidations row is marked `failed` with `error`; the explore call site is unaffected.
- Phase A LLM/parse failure → **keep the prior synopsis row**. Never overwrite with `[]`. (Today's `_build` returns `[]` on parse failure and `maybe_rebuild_synopsis` upserts it — `synopsis.py:68,89`. The structured parse plus this guard closes that hole.)
- Phase B resolver-chunk failure → that cluster is left untouched (`resolve()`'s ADD-default, `resolver.py:110-112`). Hallucinated/out-of-scope targets are already demoted to ADD upstream (`resolver.py:121-130`), which under Phase B's mapping means "do nothing".
- Store failure applying one op → log, continue with the remaining members; `ops` counts only what was applied. `insert_resolution_events` stays best-effort (`sqlite.py:1111-1112`).
- Above `max_findings_for_pairs` → skip, log, mark the row `completed` with `ops={"skipped": "kb_too_large"}`. Not an error.
- **Concurrency: last-write-wins, justified.** Both tiers write rows atomically, so an overlap with a concurrent explore can only race two `update_finding` calls on one row. The pass is **idempotent over live state**: a lost update is simply re-derived next pass, since the merge that produced it re-clusters identically. Soft guard: skip if a `running` consolidations row for the KB is younger than `running_ttl_minutes` — a TTL, not a lock, so a crashed row expires instead of wedging the KB forever.
- **Idempotency argument**: merged duplicates leave the live set (`invalidated_at` set), so their merge clusters collapse to singletons and a re-run makes ~zero resolver calls. Members the resolver called distinct stay `ADD` — a literal no-op, writing nothing. And `should_consolidate`'s debounce blocks the synopsis rebuild. A second pass over a settled KB costs one `finding_near_pairs` call and nothing else.

## Testing & acceptance criteria

1. `clusters_from_pairs`: chained pairs (a-b, b-c, c-d) form one cluster; the **same pair list** filtered at 0.80 and 0.50 yields the tight and loose clusterings; disjoint pairs stay separate; empty input → `[]`. Pure, no IO.
2. `star_members`: anchor = highest confidence, tie-break oldest `created_at`, then id; transitively-attached members (no direct anchor edge) are **excluded**; truncation to `max_cluster_members` drops the weakest edges first.
3. `should_consolidate`: false at `live_count=0`; true with no prior row; delta and age thresholds each fire independently; `force=True` bypasses; `enabled=False` short-circuits with **zero Store calls** (assert via a mock).
4. Decision→store-op mapping (temp `SQLiteStore`): `ADD` writes nothing; `NOOP` leaves the anchor live with unioned provenance, monotonically non-decreasing confidence, and **`content`/`embedding`/`title` byte-identical** (E2's None→keep), member `invalidated_at` set with `superseded_by=anchor`; `UPDATE` inverts survivor/retiree; **`SUPERSEDE` writes no `invalidated_at`** and produces exactly one `STALE_FLAG` event.
5. Phase C stale gate: at `stale_age_days=0`, a contradicted **and** ancient finding is **not** invalidated (flag only); at `stale_age_days=30`, an ancient-but-uncontradicted finding is **not** invalidated; only contradicted **and** ancient is. `accessed_at` is preferred over `created_at` as last-touch.
6. `finding_near_pairs`: against a seeded temp `SQLiteStore`, returns pairs with `a < b`, no self-pairs, all `similarity >= min_similarity`, sorted descending, and **excludes pairs touching an invalidated row**. Mirrored by a fake-client test for the RPC params on the cloud tier (style of `tests/test_supabase_findings.py`).
7. `list_finding_digests` returns **more than `LIST_MAX_LIMIT` (100)** rows on a 150-finding fixture — the direct regression test for the `limit=200`→100 truncation — on both tiers.
8. `source` parity: a consolidation-driven event round-trips `source='consolidation'` through `insert_resolution_events`/`list_resolution_events` on **both** tiers; an E2 explore-path event round-trips `'insert'` with no call-site change.
9. **LLM budget**: a fixture with 20 merge clusters and a stubbed gateway records **exactly `1 + max_clusters_per_pass` = 9** completion calls, and **zero** `embed_batch` calls (Phase B never embeds).
10. **Idempotency**: second pass over the fixture consolidated by pass 1 performs **0** `update_finding`/`invalidate_finding` calls and **0** resolver calls; `count_findings` is unchanged.
11. **Golden-set acceptance gate** (E1's harness): after a consolidation pass over each golden KB, **every golden query's expected finding ids remain reachable** — live, or via the `superseded_by` chain from the recorded id — and coverage verdicts (`rich`/`sparse`/`gap`) **must not regress**. This is the gate; a threshold change that trips it is rejected.
12. E1's band-calibration script (§C4(c) of the E1/E2 spec) is the source for `merge_cluster_min_sim` / `topic_cluster_min_sim` per embedding model. Defaults ship uncalibrated and work; calibration turns them from guesses into measured values.

## Sequencing & dependencies

**On `list_finding_digests` vs E7.** E6 and E7 (lazy KG) both need it. **Whichever lands first implements it per this spec's signature** — `list_finding_digests(kb_id, *, limit=5000, include_invalidated=False) -> list[dict]` with `id, title, category, confidence, created_at` — and the other consumes it as-is. E7 uses only the ids. Neither may add a `list_finding_titles`/`list_finding_ids` variant.

**On E2.** This spec's substrate is the [write-path dedup spec](2026-07-16-write-path-dedup-design.md), which is the authority for `ResolutionOp = ADD|UPDATE|NOOP|SUPERSEDE` (no DELETE), `valid_from`/`invalidated_at`/`superseded_by`, the `supersede_finding`/`invalidate_finding`/`update_finding`(None→keep) primitives, live-filtered `match_findings`/`count_findings`, and `resolution_events.new_finding_id`/`details`. None of it exists on `feat/mem0-resolution-port` — the E2 **spec** introduces all of it, and E6 targets that state, not the branch's.

| Step | Lands | Needs |
|---|---|---|
| 1 | E2 Rollout 1-2 | — |
| 2 | **E6 Phase A** + C1 Store methods + `consolidations` table | E2 **§C3 only** (the three columns + live filters) |
| 3 | **E6 C6** triggers: tool, route, scheduler | step 2 |
| 4 | E2 Rollout 3-5 (incl. **§C6 SupabaseStore parity** + migration; cloud guard removed) | — |
| 5 | **E6 C7** (`source`, both tiers) + **Phases B/C** | E2 §C2 + §C6 |

- **Phase A is standalone of the resolver** — it needs no `resolve()`, no write primitives, no C6. It needs only E2 §C3's columns for its live predicates, which land at E2 Rollout step 2 (early). If E6 must ship before that, Phase A drops the `invalidated_at` predicates from `finding_near_pairs` and `list_finding_digests` — a one-line change per site, re-added by §C3. Everything is live in that world, so behavior is identical.
- **Phases B/C require E2 §C6 on cloud.** Until C6 lands, `SupabaseStore` has no `update_finding` / `invalidate_finding` / `insert_resolution_events` and no cloud `resolution_events` table exists, and E2's **cloud guard forces pure-ADD while `active_backend() == "cloud"`** (E2 §Architecture; `store/__init__.py:60-69`). Phases B/C honor the same guard: `run_consolidation` runs **Phase A only** on cloud until the guard is removed at E2 Rollout step 5. This is a named dependency, not a tier-parity exemption — the cloud SQL in §Store changes ships in full at step 5.
- **Migration order is strict.** E2's migration **CREATEs** `resolution_events` (its §Migration); E6 only **ALTERs** it. E6's migration file must sort after E2's, and E6's `source` work is in step 5 for exactly this reason. Same on SQLite: E2 §C3 adds `new_finding_id`/`details` to both `_SCHEMA` and `_ADD_COLUMN_MIGRATIONS`; E6 appends `source` to both, on top.
- **E1 is optional**: defaults work uncalibrated. E1 supplies acceptance #11's harness and #12's calibration.

## Open risks

1. **Single-link chaining welds distinct topics into one merge cluster.** Union-find at 0.80 links `a-b` and `b-c` even when `a`/`c` are unrelated. *Resolution:* **star reduction** (§C2) structurally removes it from Phase B — only members with a **direct measured edge to the anchor** are resolved, so a transitively-chained member is never a merge candidate and is never handed to the resolver with an invented similarity. Chaining persists in Phase A's 0.50 topic clusters, where it is harmless (a slightly broad topic grouping in a prompt) and bounded by `titles_per_cluster`.
2. **`finding_near_pairs` is O(n²) on both tiers.** No index helps a self-join; sqlite-vec's ANN path needs a per-row `MATCH` KNN query, which would trade one statement for n. *Resolution:* accept the brute-force scan — it is the same shape as today's `match_findings` (`sqlite.py:239-247`), runs in C, in the background, at most once per `debounce_delta=25` inserts — and bound it with `max_findings_for_pairs = 5000` (≤12.5M distance computations), above which the pass skips cleanly. If a KB outgrows that, the fix is per-anchor KNN behind the same Store signature; no caller changes.
3. **The API route blocks for the length of the pass** (~seconds to ~1 min at the 9-call budget cap) and may hit client timeouts. *Resolution:* accept for v1 — the budget is hard-bounded by `max_clusters_per_pass`, and the MCP tool and the post-explore scheduler (the two paths that actually matter) are respectively agent-driven and detached. If it bites, `POST /consolidate` returns the consolidation id immediately and clients poll `get_last_consolidation`; the lifecycle row already carries `status`/`ops`/`error` for exactly that.
