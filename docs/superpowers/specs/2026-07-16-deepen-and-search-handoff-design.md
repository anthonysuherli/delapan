# Deepen wiring + two-phase agent search handoff — design spec

*2026-07-16 · Status: approved design, pre-implementation · Line: delapan master · Portfolio item E5 from `delapan-ai/reports/2026-07-15-delapan-enhancement-brainstorm.md` · Every file:line below re-verified against `master` (the worktree currently sits on `feat/write-path-dedup`; cites were read via `git show master:<path>`). Cloud facts verified live against `gunqbyddzuwzpncfigro` on 2026-07-16.*

**PROBLEM:** `run_deepen` is wired to nothing, and a missing `TAVILY_API_KEY` makes explore return zero findings in silence. **SOLUTION:** consume the already-documented `search_mode` at the tool boundary — hand the search to the calling agent and resume through the `ingest_pages` seam — and expose deepen as a store-backed job with a status tool and an SSE route.

## Context / Problem

Three defects, one root cause — the engine's search dependency is never resolved at any boundary:

1. **Deepen is dead code.** `run_deepen` (`core/exploration/deepen.py:145`) is complete and tested-by-eye but reachable from nothing: `git grep run_deepen` on master hits only `deepen.py` itself and `core/config.py` (which ships a full `DeepenConfig` at `config.py:310-336` plus a `deepen:` block at `config.yaml:112-121`). Knobs for a feature with no caller.
2. **A missing key fails silently.** `tavily.search` is wrapped in `@_with_retry(max_retries=3, base_delay=1.0, fallback=list)` (`core/clients/tavily.py:53`), so a keyless call retries four times, logs one warning, and returns `[]`. `run_exploration` then hits `if not search_results:` → `progress("completed")` → `return []` (`engine.py:100-102`). `delapan_explore` reports `{"count": 0}` and marks the row `completed`. The KB is untouched and nothing says why.
3. **`search_mode` is validated but never read.** `ExplorationConfig.search_mode` (`config.py:248`) has a field validator (`config.py:250-255`) and a documented three-way contract (`config.py:243-247`), mirrored in `config.yaml:77`. No code branches on it. The documented promise — *"`auto` — Tavily if `TAVILY_API_KEY` is set, else hand the search to the calling agent (only when the host can fulfill it)"* — is unimplemented.

**The seam already exists.** `ingest_pages` (`engine.py:135-176`) is the keyless back half — extract → evaluate → merge — and its docstring designates this exact use: *"Shared by `run_exploration` (Tavily content) and the agent-handoff path (host-fetched content)"* (`engine.py:147-149`). It is already exported from `delapan.core.exploration` (`__init__.py`). E5 builds the front-half fork and the job shell around `run_deepen`; nothing in the engine's back half changes.

**The cloud schema already anticipated it.** Verified live: `explorations` carries a nullable **`plan jsonb`** column, and its status CHECK already admits **`awaiting_ingest`**:

```
explorations_status_check CHECK (status = ANY (ARRAY[
  'pending','planning','searching','crawling','extracting','merging',
  'awaiting_ingest','completed','failed']))
```

Neither is referenced anywhere in the engine (`git grep awaiting_ingest master` → no hits). They are dormant affordances from the delapan lineage. **E5 activates them rather than inventing parallel names** — this materially shrinks the migration and is why the handoff status is `awaiting_ingest`, not a new `awaiting_search`.

## Decisions (locked)

1. **Two-phase agent search handoff.** No Tavily key + agent mode on a host that can search → return a structured `search_request` (the planned queries) and persist the plan in an `explorations` row; `delapan_explore_continue(pages)` resumes through `ingest_pages`.
2. **Job-style deepen.** `delapan_deepen` returns a job id immediately; `delapan_deepen_status(job_id)` reports progress and returns the final digest; an SSE route streams the same progress.
3. **Exactly three new MCP tools**: `delapan_explore_continue`, `delapan_deepen`, `delapan_deepen_status`.
4. **`search_mode`'s documented semantics are honored, not redefined** (see §C1). `auto` + no key hands off **on the MCP surface** and errors **on HTTP**.
5. **One persist seam: E2's.** E5 creates no persist module of its own (§C8).
6. **Independent of E1/E2/E3, buildable on master today.** Degradations stated in §Sequencing.

## Design

`explorations` rows become the durable state for both features: the handoff's parked plan and the deepen job's progress. That is what makes the MCP surface stateless between calls and lets an SSE route in a *different* process watch an MCP-started job.

```
delapan_explore ──► resolve_search_backend(cfg, settings, surface="mcp")
   ├─ "tavily" ─► run_exploration ──► persist ──► row completed          (unchanged)
   └─ "agent"  ─► plan_queries ─► row{status=awaiting_ingest, plan} ─► return search_request
                     │                    (host runs WebSearch/WebFetch per SKILL.md)
                     └─► delapan_explore_continue(exploration_id, pages)
                            ─► pages_to_content ─► ingest_pages ─► stamp via="agent"
                            ─► resolve_and_persist ─► row completed ─► synopsis + KG
   (raise) ◄── tavily mode + no key, anywhere · agent/auto + no key on HTTP

delapan_deepen ──► resolve_search_backend(...) must be "tavily", else raise
                ─► row{kind=deepen, status=running} ─► asyncio task (strong-ref set)
                     run_deepen: on_progress → patch row.progress
                                 on_round    → slice to cap → resolve_and_persist
delapan_deepen_status(job_id) ─┐
GET …/deepen/{id}/events (SSE) ─┴─► get_exploration → same progress JSON → digest
```

Deepen cannot use the handoff: `run_deepen` drives an autonomous multi-round loop with no turn boundary at which to return control to the host. It therefore hard-requires Tavily, checked once at entry (§C5) rather than discovered as `[]` per facet.

## Components

### C1 · `resolve_search_backend` — `delapan/core/exploration/handoff.py` (new)

```python
def resolve_search_backend(
    cfg: ExplorationConfig, settings: Settings, *, surface: Literal["mcp", "http"]
) -> Literal["tavily", "agent"]:
```

`surface` encodes the host-capability condition the config comment already states — *"only when the host can fulfill it"*. `mcp` means an LLM host with WebSearch/WebFetch is on the other end of the call; `http` means an API client that cannot be asked to run a search mid-request. Without this parameter the documented contract is unimplementable.

| `search_mode` | `TAVILY_API_KEY` | `surface="mcp"` | `surface="http"` |
|---|---|---|---|
| `tavily` | set | `tavily` | `tavily` |
| `tavily` | unset | **raise** | **raise** |
| `auto` | set | `tavily` | `tavily` |
| `auto` | unset | **`agent`** | **raise** |
| `agent` | set | `agent` | **raise** |
| `agent` | unset | `agent` | **raise** |

Raises `SearchBackendUnavailable(ValueError)` naming the mode, the surface, and the fix. No config text changes: this table *is* `config.py:243-247` + `config.yaml:77` as written.

Also in this module:
- `pages_to_content(pages, plan, cfg) -> tuple[dict[str, str], dict[str, str]]` — normalize agent-supplied pages into the two dicts `ingest_pages` takes. Drops entries missing `url` or `content`; caps to `cfg.max_pages` (`config.py:269`); truncates each body to `cfg.max_content_per_page` (`config.py:271`); maps `url → query` from each page's `query`, falling back to `plan.search_queries[0].query`.
- `stamp_via(findings, via="agent") -> None` — set `via` on every provenance entry in place. Findings are stamped by `_build_findings` as `[{"url", "query"}]` (`engine.py:319`); stamping at the tool keeps `engine.py` untouched, and E2's `normalize_provenance` preserves unknown keys.
- `handoff_expired(row, cfg) -> bool` — `now - datetime.fromisoformat(row["created_at"]) > cfg.handoff_ttl_hours`.

### C2 · Explore fork — `delapan/mcp/server.py` (edit)

Inside `delapan_explore` (`server.py:132-195`), before `create_exploration`: resolve the backend. `"tavily"` → today's path, byte-for-byte. `"agent"` → plan only, park the row, return:

```jsonc
{"status": "awaiting_ingest", "exploration_id": "…",
 "search_request": {
   "queries": [{"query": "…", "domain_filter": "", "priority": 1, "max_results": 20}],
   "max_pages": 15, "max_content_per_page": 100000,
   "instructions": "Run each query (WebSearch), fetch the best results (WebFetch), then call delapan_explore_continue(project, kb, exploration_id, pages=[{url, title, content, query}])."}}
```

The plan comes from `plan_queries(prompt, cfg, lens="explore")` (`planner.py`, called at `engine.py:85`) and is parked with `update_exploration(exp_id, status="awaiting_ingest", plan=plan.model_dump())`. `ExplorationPlan` (`models.py:35-39`) nests only `SearchQuery` (`models.py:28-32`, all `str`/`int`), so `model_dump()` is JSON-safe with no encoder.

### C3 · `delapan_explore_continue(project, kb, exploration_id, pages)` — new tool

1. `ctx = resolve_tenant(project, kb, create=False)`; `store = get_store(...)`.
2. `row = store.get_exploration(exploration_id)` — reject if missing, if `row["status"] != "awaiting_ingest"`, if `handoff_expired(row, cfg)`, or if **`row["kb_id"] != ctx.kb_id`**. The last check is why `kb_id` joins the widened select (§Store): cloud RLS scopes reads to the *org*, not the KB, so without it a caller could ingest into a sibling KB's row.
3. `plan = ExplorationPlan.model_validate(row["plan"])`.
4. `content_by_url, url_to_query = pages_to_content(pages, plan, cfg)`.
5. `findings = await ingest_pages(content_by_url, url_to_query, plan, exploration_id=exploration_id, project_id=ctx.project_id, kb_id=ctx.kb_id, cfg=cfg)` — the seam, unmodified.
6. `stamp_via(findings, "agent")`; `captured = findings[:cap]` with `cap = min(max_findings or cfg.default_max_findings, cfg.max_findings)` — the same cap expression as `server.py:143`.
7. `outcome = await resolve_and_persist(ctx, store, captured, get_config())` (§C8); `update_exploration(..., status="completed", completed_at=…, finding_ids=ids)`; then `await maybe_rebuild_synopsis(...)` and `schedule_kg_update(ctx, ids, store=store)` — mirroring `server.py:189-190`, so a continued exploration grows the stable layers exactly like a Tavily one.

### C4 · Deepen job registry — `delapan/mcp/jobs.py` (new)

Follows the strong-ref fire-and-forget pattern of `schedule_kg_update` (`knowledge_graph/builder.py:257-267`) — a module-level `set` holds the task, `task.add_done_callback(_JOBS.discard)` releases it — so a job is never GC'd mid-flight.

- `start_deepen_job(ctx, store, topic) -> str` — create the row, patch `kind="deepen"`, `status="running"`, seed `progress`, spawn the task, return the row id as the job id.
- `job_alive(job_id) -> bool` — is a task for this id live *in this process*.

Job body callbacks:
- `on_progress(phase)` → `update_exploration(job_id, progress={**cur, "phase": phase, "updated_at": now})`.
- `on_round(rr)` → `captured = rr.findings[: cfg.max_findings_per_round]`; `resolve_and_persist`; accumulate ids; patch `progress` with `{round, facets, coverage, findings_so_far, updated_at}` and `finding_ids`. **Per-round persistence is the durability guarantee**: a job that dies at round 3 leaves rounds 1–2 in the KB, real and inspectable.
- `coverage_probe=None` — `run_deepen` already treats the probe as optional (`deepen.py:216`) and still terminates on `done` / `coverage_target` / no `next_facets` / `depth_cap`. Wiring a probe to `match_findings` + `assess_coverage` belongs to E4, which owns coverage.

On completion: build the digest, `update_exploration(status="completed", completed_at, finding_ids=all_ids, progress={…, "digest": digest})`, then synopsis + KG. On exception: `status="failed"`, `error=str(exc)` — the `server.py:191-193` pattern.

### C5 · `delapan_deepen` / `delapan_deepen_status` — new tools

`delapan_deepen(project, kb, topic) -> {"job_id", "status": "running"}`. **First statement is the guard**: `resolve_search_backend(cfg, settings, surface="mcp")`, and if it is not `"tavily"`, raise — *"deepen requires TAVILY_API_KEY: the autonomous loop cannot hand a search back to the caller mid-loop; use delapan_explore for the agent-handoff path."* Without this guard E5 reintroduces its own framing bug on a new surface: `run_deepen` → `_facet` → `run_exploration` (`deepen.py:176-182`) → `tavily.search` → `[]` → every round completes empty until `depth_cap` and the job is marked `completed`.

`delapan_deepen_status(job_id) -> dict`. Takes no project/kb: it uses `resolve_store()` (`mcp/tenancy.py:114-126`), the org-scoped store with no project/KB binding already used by `delapan_projects` (`server.py:205`); cloud RLS scopes the read to the caller's org. Returns by `row["status"]`: `completed` → `progress["digest"]`; `failed` → the error; `running` → `progress`, unless `not job_alive(job_id)` **and** `now - progress["updated_at"] > cfg.orphan_after_seconds`, in which case `{"status": "orphaned", …}` plus the ids persisted so far. `orphaned` is **derived at read time, never stored** — no schema/CHECK cost, and a job that is merely slow is never mislabeled.

### C6 · Progress threading — `delapan/core/exploration/deepen.py` (edit)

The only engine edit. `_facet` (`deepen.py:174-182`) passes no `on_progress`, so a deepen job is blind between `round-{r}-searching` markers (`deepen.py:185`). Give `_facet` the round index and forward a facet-scoped callback into `run_exploration`, emitting `round-{r}/{phase}` over the engine's own phase names (`engine.py:8-9`). *Departure from the E5 draft*: the round slice stays **out** of `deepen.py` and lives in the job's `on_round` (§C4) — the persistence cap belongs to the call site, exactly as `findings[:cap]` does at both existing explore call sites. `deepen.py` stays pure of persistence policy.

### C7 · HTTP surface — `delapan/api/routes_deepen.py` (new), mounted in `api/main.py`

- `POST /api/projects/{p}/kbs/{k}/deepen` → `{"job_id"}`. Guard: `resolve_search_backend(cfg, settings, surface="http")` must return `"tavily"`, else the loud error — this is a superset of `routes_explore.py`'s `_missing_keys()` check (`routes_explore.py:54-62`), which stays as-is on its own route.
- `GET /api/projects/{p}/kbs/{k}/deepen/{job_id}/events` → SSE, reusing `routes_explore.py:50-51`'s `_sse` framing. Polls `get_exploration` every `sse_poll_seconds`, emits only when the progress JSON changes, and emits a terminal event on `completed`/`failed`.

**Why polling.** The store is the only thing both processes share. Polling is tier-uniform (SQLite has no pub/sub) and process-agnostic — the one mechanism that can stream a job the MCP stdio process started. A queue would be strictly better *and* only work for jobs started inside the API process, which is the case that matters least.

### C8 · Persist seam — E2's, at E2's address

**Decision: E5's new call sites call `resolve_and_persist(ctx, store, candidates, get_config())` — E2's exact signature and address (`delapan/core/memory/persist.py`). E5 creates no seam of its own.** The E5 draft's `core/exploration/persistence.py::persist_findings(store, *, org_id, kb_id, …)` is dropped: it could not have delegated to `resolve_and_persist(ctx: TenantContext, store, candidates, cfg: AppConfig)` without fabricating a `TenantContext` (`core/agent/state.py:28-37`: `user_id, org_id, project_id, kb_id, thread_id, access_token`), and it would have re-lifted a block that `feat/mem0-resolution-port` commit `6731774` already deleted from both call sites — a guaranteed conflict producing two competing "single seams".

**Pre-E2 fallback.** If E5 lands first, it ships that module as a stub at E2's address whose body is **E2's own kill-switch fast path** (`persist.py:72-75` on the branch): render → `embed_batch` → `insert_findings` → `ResolutionOutcome(affected_finding_ids=ids)`. This is not a competing seam — it is the same function, same address, same signature, with only the pure-ADD arm implemented, and it is behaviorally identical to today's inline block at `server.py:156-181`. Render helpers come from `core/exploration/render.py` — again E2's address (branch commit `c002bd8`), not a new one.

**Merge resolution, stated:** when `feat/mem0-resolution-port` merges, `core/memory/*` and `core/exploration/render.py` conflict as add/add and resolve **take-theirs, whole-file**. E5's call sites need **zero** edits and gain resolution the moment E2 lands. E5 does **not** touch the existing duplicated `_render_content`/`_normalize_provenance` in `server.py:46-90` / `routes_explore.py:65-105` — deleting those is E2's DRY refactor (`6731774`) and doing it here would collide.

Two consequences to state plainly: pre-E2, continued and deepened findings dedupe by `FindingMerger` only — today's behavior for every finding, no regression. Post-E2 but pre-C6, the write-path spec's **cloud guard forces pure ADD while `active_backend() == "cloud"`**, so continuation and deepen persist unresolved on the cloud tier until E2's C6 lands. Neither blocks E5.

### C9 · Docs + plugin surface — engine `README.md` + wrapper `/Users/anthonysuherli/Repositories/8star/delapan-ai`

E5 takes the MCP surface from four tools to **seven**, and the plugin from four skills to **five**. The four-tool/four-skill surface is hardcoded in five places; the tools ship broken-in-docs without all of them:

- `delapan/mcp/server.py:6-12` — module docstring: *"deliberately small — four tools"* + the four-line tool list.
- `README.md:24-26` — the prose tool list, which also says `delapan_explore` *"needs LLM + Tavily keys"* — now false by default (`search_mode: auto` hands off).
- `README.md:96` — *"all 4 tools register and run"*.
- Wrapper `.claude-plugin/plugin.json` — the `skills` array (four `./skills/*` entries) **and** the `description`, which enumerates the four `/delapan:*` skills; plus `.claude-plugin/marketplace.json`'s plugin `description`, which enumerates them again.
- Wrapper `skills/explore/SKILL.md` (edit) + `skills/deepen/SKILL.md` (**new**, and registered in `plugin.json`'s `skills` array — without that it ships with no `/delapan:deepen` slash command).

Only **`deepen` becomes a skill**. `delapan_explore_continue` and `delapan_deepen_status` are called *within* the explore and deepen workflows, not as standalone slash commands — 7 tools, 5 skills.

- `skills/explore/SKILL.md` (edit) — the front matter and Prerequisites currently promise *"Blocks ~1–3 minutes. Requires LLM and Tavily keys"* and *"`TAVILY_API_KEY` for web search"* / *"If keys are missing, tell the user which env vars to set — do not silently skip"* (`SKILL.md:3, 24-29`). Rewrite: Tavily becomes **optional**; document the `search_request` response shape and the WebSearch/WebFetch → `delapan_explore_continue` loop, including the `max_pages`/`max_content_per_page` caps the host must respect and the `handoff_ttl_hours` window.
- `skills/deepen/SKILL.md` (new) — submit → poll `delapan_deepen_status` → present the digest; state that Tavily **is** required here and why (no mid-loop handoff), and steer long runs to the HTTP route when the backend server is up.

**Collision flag — E4 (curation flywheel), and it is deeper than docs.** The sibling E4 spec has flagged E5 symmetrically; both flags are accurate. Overlap: (1) all five surfaces above — E4 adds `delapan_backlog` and `skills/backlog/SKILL.md`, so the tool count and both manifests are contended; (2) **`skills/explore/SKILL.md`**, which E4 also edits (promptless-consume note); (3) **`delapan_explore` itself** — E4 makes `prompt` optional and inserts a backlog-consume path *before* the pipeline (`server.py:133`, `145-195`), while E5's §C2 forks on the search backend inside the same function. That third one is a genuine code conflict, not a docs merge. Land order is free; the second lander rebases and reconciles the count to **7 tools / 5 skills** (E4 alone → 5/5; E5 alone → 7/5). Neither spec owns those files, and neither should rewrite the docstring or `delapan_explore` blind.

**Drift, flagged not fixed:** `CLAUDE.md` names `delapan-ai/docs/technical-overview.md` as authoritative and house rules require updating its "Current state" in the same change — **that file does not exist** in either repo. `README.md` is the real doc surface. (E4 independently found and flagged the same stale pointer.) Do not create the file to satisfy the rule.

## Store changes

**No new Protocol methods.** The existing exploration lifecycle (`store/base.py:86-98`) widens in place; `create_exploration` is unchanged.

- `update_exploration` — allowed-key set gains `kind`, `plan`, `progress` (`sqlite.py:433`; `supabase.py:443`).
- `get_exploration` — the select widens from `id, status, finding_ids, completed_at, error` to additionally `kind, plan, progress, prompt, kb_id, created_at, started_at` (`sqlite.py:447-465`; `supabase.py:449-458`). Update the `sqlite.py:452-454` docstring that enumerates the returned keys.
- **`created_at` is the TTL anchor** (§C1 `handoff_expired`). It is selected, not synthesized: today's row shape has no timestamp an `awaiting_ingest` row would populate (`completed_at` is NULL until terminal), so the draft's `handoff_ttl_hours` had nothing to compare against. Both tiers already write `created_at` on insert (`sqlite.py:412-424`; `supabase.py:434-440`) — SQLite as `_now_iso()` text, cloud as `timestamptz` rendered by PostgREST as ISO-8601. Both parse with `datetime.fromisoformat`; both are tz-aware. Anchoring on an existing column beats stamping a timestamp into `plan`, which would duplicate a truth the row already holds.
- **Status vocabulary** gains `awaiting_ingest` (already in the cloud CHECK — free) and `running` (needs the CHECK migration below).

### SQLite (`delapan/store/sqlite.py`)

Append to `_ADD_COLUMN_MIGRATIONS` (`sqlite.py:121-126`), applied idempotently in `_ensure_schema` (`sqlite.py:184-196`, which swallows *duplicate column name*). Note `plan` is **SQLite-only** — cloud already has it, so this closes an existing divergence:

```python
# 0009: two-phase handoff + deepen jobs
"ALTER TABLE explorations ADD COLUMN kind TEXT DEFAULT 'explore';",
"ALTER TABLE explorations ADD COLUMN plan TEXT;",      # JSON ExplorationPlan
"ALTER TABLE explorations ADD COLUMN progress TEXT;",  # JSON job progress
```

Both are constant defaults, which SQLite's `ALTER TABLE ADD COLUMN` permits (unlike `now()`), so pre-existing rows read back `kind='explore'`.

**Two SQLite-specific requirements the column adds do not satisfy** — return-shape parity is load-bearing (`sqlite.py:11-13`):

1. **Write:** `update_exploration` JSON-encodes *only* `finding_ids` — `vals.append(json.dumps(v) if k == "finding_ids" else v)` (`sqlite.py:440`). Passing a `plan`/`progress` dict binds a `dict` parameter and `sqlite3` raises `InterfaceError`. The predicate must widen to `if k in {"finding_ids", "plan", "progress"}`. Cloud needs no equivalent: PostgREST coerces dicts to `jsonb` natively (per the SupabaseStore spec §6.5) — **never** `json.dumps` on that tier.
2. **Read:** `get_exploration` must `_json_load(r["plan"], None)` and `_json_load(r["progress"], None)` (`sqlite.py:1066`) to match Supabase's already-decoded `jsonb`.

**`kind` normalization.** Both stores return `row["kind"] or "explore"` — the column default handles new and migrated rows, and the read-side coalesce handles any row that stored an explicit NULL. Without it the same logical row reads `None` locally and `'explore'` on cloud, and any `kind == "explore"` test behaves per-tier.

### Supabase migration (applied to the cloud project; lives outside this repo)

```sql
-- 00XX_explorations_handoff_and_jobs.sql
-- Verified live on gunqbyddzuwzpncfigro (2026-07-16): `explorations` ALREADY has
-- `plan jsonb` (nullable) and its status CHECK ALREADY admits 'awaiting_ingest'.
-- Both are dormant — no engine code references them. Only `kind`, `progress`,
-- and the 'running' status are new. ids are uuid; finding_ids is uuid[] NOT NULL
-- DEFAULT '{}' (an array column, not jsonb — do not JSON-encode it).

alter table explorations
  add column if not exists kind text not null default 'explore',
  add column if not exists progress jsonb;

comment on column explorations.kind is
  'explore | deepen — which surface owns this row';
comment on column explorations.plan is
  'ExplorationPlan snapshot for awaiting_ingest handoff rows; TTL anchors on created_at';
comment on column explorations.progress is
  'deepen job progress {round, phase, facets, coverage, findings_so_far, updated_at, digest?}';

-- Status CHECK: keep every existing value verbatim, add 'running' for in-flight
-- deepen jobs. 'awaiting_ingest' is already present and is reused as-is.
alter table explorations drop constraint if exists explorations_status_check;
alter table explorations add constraint explorations_status_check
  check (status = any (array[
    'pending','planning','searching','crawling','extracting','merging',
    'awaiting_ingest','running','completed','failed'
  ]));
```

**RLS: no policy changes.** Verified live — `explorations` carries four membership-based policies (`explorations_select/insert/update/delete`), each qualified `org_id in (select org_id from org_members where user_id = auth.uid())`. They are column-agnostic and already cover `kind`/`plan`/`progress`. **No RPC changes:** `explorations` is reached through the PostgREST table API only (`supabase.py:434-458`), never a deployed function.

## Config

Every knob lands in a pydantic section, mirrors into `config.yaml`, and is overridable via `DLP_<SECTION>__<FIELD>` (`config.py:19-20, 407`).

| Section | Knob | Default | Meaning |
|---|---|---|---|
| `ExplorationConfig` (`config.py:229`) | `handoff_ttl_hours` | `24` | `explore_continue` rejects an `awaiting_ingest` row older than this (anchored on `created_at`). |
| `DeepenConfig` (`config.py:310`) | `max_findings_per_round` | `24` | Per-round persistence cap, applied in the job's `on_round`. |
| `DeepenConfig` | `sse_poll_seconds` | `2.0` | Store-poll interval for the deepen SSE route. |
| `DeepenConfig` | `orphan_after_seconds` | `300` | A `running` row this stale with no live in-process task reports `orphaned`. |

`search_mode` is **not** a new knob and its meaning is **not** changed — E5 is the first code to read it (`DLP_EXPLORATION__SEARCH_MODE`). The handoff deliberately reuses `max_pages` (`config.py:269`) and `max_content_per_page` (`config.py:271`) rather than adding agent-specific caps: the same ceilings should bound host-fetched and Tavily-fetched content, and a second pair would drift.

## Error handling

- **`tavily` mode + no key, any surface** → `SearchBackendUnavailable` before any row is created. **`auto`/`agent` + no key on HTTP** → same, naming the surface: the API client cannot run the search. `auto` + no key on **MCP** is not an error — it is the handoff (§C1).
- **Deepen + non-`tavily` resolution, any surface** → raise at entry, before the row exists (§C5). Never discovered per-facet.
- **`explore_continue` against a missing / non-`awaiting_ingest` / expired / wrong-KB row** → error dict naming the id, the actual status, and the TTL. Row untouched.
- **`explore_continue` with zero usable pages after normalization** → error returned; row **stays** `awaiting_ingest` so the agent can retry with better pages. It is not a failure of the exploration.
- **Facet failure inside deepen** → already tolerated: `gather(..., return_exceptions=True)` + warning (`deepen.py:186-192`).
- **Persist/embed failure mid-job** → the task marks the row `failed` with the error (`server.py:191-193` pattern). Prior rounds stay persisted — per-round persistence, not a rollback.
- **MCP process dies mid-job** → the row is stuck `running`; `deepen_status` derives `orphaned` from registry-miss + stale `updated_at`. The partial KB state is real and inspectable, not phantom.
- **SSE client disconnect** → the generator is cancelled; the job is store-backed and unaffected.
- **API errors** propagate as today; `record_access`-style best-effort semantics are untouched.

## Testing & acceptance criteria

1. **Backend-resolution matrix**: all 12 cells of §C1's table assert exactly — including `auto` + no key + `surface="mcp"` → `"agent"` (the cell that encodes the documented contract) and `auto` + no key + `surface="http"` → raise.
2. **Handoff round-trip** on an in-memory `SQLiteStore` with a stubbed planner/extractor: `pending → awaiting_ingest → completed`; the parked `plan` round-trips through `model_dump`/`model_validate` unchanged.
3. **Shape parity**: a fixture page-set pushed through `ingest_pages` yields finding rows identical to the Tavily path except `provenance`, where every entry carries `via: "agent"`.
4. **Continue rejection paths**: missing id, wrong status, `created_at` older than `handoff_ttl_hours`, `kb_id` mismatch, and zero usable pages each return an error and leave the row's status unchanged (`awaiting_ingest` in the zero-pages case).
5. **Page normalization**: `> max_pages` pages are truncated to `max_pages`; a body over `max_content_per_page` is cut to exactly that; entries missing `url` or `content` are dropped.
6. **Deepen guard**: with `TAVILY_API_KEY` unset, `delapan_deepen` and `POST …/deepen` both raise **before** `create_exploration` — asserted by the store recording zero exploration rows. This is the regression test for the framing bug on the new surface.
7. **Deepen job** with a faked `run_deepen`: `progress` patched per round, `finding_ids` accumulate per round, digest present at completion, `status="failed"` + error on raise.
8. **Orphan detection**: clear the registry and back-date `progress.updated_at` beyond `orphan_after_seconds` → `deepen_status` reports `orphaned` and still returns the ids persisted so far.
9. **Store round-trip, both tiers** (Supabase via the fake client of `tests/test_supabase_findings.py`): `update_exploration(plan={...}, progress={...}, kind="deepen")` then `get_exploration` returns **equal dicts on both tiers** — the SQLite path proves `json.dumps` on write + `_json_load` on read, the cloud path proves no double-encoding. A pre-migration SQLite DB opened after the migration reports `kind == "explore"`, not `None`.
10. **Migration applied** to the cloud project; a row inserted with `status='running'` and one with `status='awaiting_ingest'` both satisfy the CHECK, and `kind` defaults to `'explore'`.
11. **Untrusted-page containment** (§Risk 1): a fixture page whose body contains extraction-prompt-injection text produces either no finding or a finding the evaluator scores below `min_confidence_threshold` — and, if any survives, its provenance is `via: "agent"` and therefore filterable.
12. **Docs + plugin surface**: `grep -rn "four tools\|all 4 tools" delapan/ README.md` returns nothing; `plugin.json`'s `skills` array has five entries and `/delapan:deepen` resolves; no doc still claims `delapan_explore` requires `TAVILY_API_KEY`.
13. **E1 leverage (deferred, not gating)**: run a golden KB's query set before and after a recorded continuation — the coverage verdict must move `gap → sparse/rich`, and handoff-sourced findings must band like Tavily-sourced ones under E1's calibration, proving the continuation path feeds the same retrieval quality.

## Sequencing & dependencies

Buildable on master today; independent of E1/E2/E3.

1. `handoff.py` (`resolve_search_backend` + `pages_to_content` + `stamp_via`) → verify: §1, §5.
2. Store widening, both tiers + the Supabase migration → verify: §9, §10.
3. Explore fork → `search_request` → verify: §2.
4. `delapan_explore_continue` → verify: §3, §4, §11.
5. `jobs.py` + `deepen.py` progress threading → verify: §7, §8.
6. `delapan_deepen` / `delapan_deepen_status` → verify: §6.
7. `routes_deepen.py` + `main.py` mount → verify: §6 (HTTP arm).
8. Docs + plugin surface — `server.py` docstring, `README.md:24-26` + `:96`, both `.claude-plugin` manifests, `skills/explore/SKILL.md`, new `skills/deepen/SKILL.md` — in the same change (house rule: docs move with behavior) → verify: §12, and §C9's E4 collision reconciled.

**Degradations, stated:**
- **E2 absent** → §C8's stub; continuation and deepen dedupe by `FindingMerger` only, i.e. today's behavior for every finding. No data-shape conflict: E2 adds columns to `findings`, E5 adds columns to `explorations`.
- **E2 present, C6 absent** → the write-path spec's cloud guard forces pure ADD on the cloud tier. E5 works; resolution simply waits.
- **E1 absent** → only acceptance §12 waits; §1–§11 gate without it.
- **E4** → §C9's `server.py` docstring + `skills/explore/SKILL.md` touchpoint.

## Open risks

1. **Agent-supplied page content is untrusted** — it reaches an LLM extraction prompt, so a hostile page can attempt prompt injection into `extract_findings`. This risk is *not new in kind* (Tavily fetches arbitrary web pages into the same prompt) but it is new in *provenance*: the fetch is no longer ours. **Resolution:** treat pages strictly as data. Keep the evaluator pass on (`enable_evaluation: true`, `config.py:277`) so vacuous/injected findings are scored down and dropped under `min_confidence_threshold` (`config.py:284`); enforce `max_pages`/`max_content_per_page` on the way in (§C1); and stamp `via: "agent"` on every provenance entry (§C3) so these findings are auditable and filterable after the fact. Acceptance §11 is the regression test. Accepted, not eliminated — the honest bound is "same exposure as the Tavily path, plus attribution".
2. **MCP stdio lifetime makes deepen jobs mortal.** A job outlives neither the client session nor a crash. **Resolution:** accept, with documented orphan semantics (§C5) and per-round persistence (§C4) so a dead job leaves real findings rather than nothing; `skills/deepen/SKILL.md` steers long runs to the HTTP route when the backend server is up. A supervisor is worth building only if observed orphan rates justify it.
3. **SSE-by-polling costs up to `sse_poll_seconds` of latency** and cannot stream sub-second. **Resolution:** accept. It is the only mechanism that is both tier-uniform (SQLite has no pub/sub) and process-agnostic (an API-hosted stream can watch an MCP-started job). Upgrading the cloud tier to Supabase Realtime is a later, additive option if the frontend ever demands it.

## Out of scope

E1 calibration, E2 resolution semantics, E3 hybrid retrieval, E4's flywheel and its `coverage_probe` wiring, E6/E7. Changing `search_mode`'s documented contract. Deleting the pre-E2 duplicated render helpers (E2's `6731774` owns that). br8n hand-port (fork rules; br8n excludes the `/v1` + API deploy surfaces this spec's C7 extends).
