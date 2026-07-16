# Lazy KG scaffold + findings-as-nodes PPR reranking — design spec

*2026-07-16 · Status: approved design, pre-implementation · Line: master · Portfolio item E7 from `delapan-ai/reports/2026-07-15-delapan-enhancement-brainstorm.md` · Adversarially verified against the codebase and the live cloud catalog (`gunqbyddzuwzpncfigro`); 10 findings folded in.*

**PROBLEM:** the KG is gated behind an expensive eager LLM pass that only intent-schema KBs ever get (`builder.py:233-234`), and once built it never helps retrieval. **SOLUTION:** a cheap pure-python NLP scaffold written at ingest for every KB, LLM extraction deferred to view time and cached by contributing-finding-set hash, plus an opt-in Personalized-PageRank reranker that reorders the vector candidate pool over that scaffold.

## Problem

Three facts, verified:

1. **The KG only grows for intent KBs.** `_auto_kg_update` returns early unless `get_kg_intent` finds an approved ontology (`builder.py:233-234`). The intent-less majority of KBs have an empty graph forever unless someone runs a manual `build_graph`.
2. **The build is eager and expensive.** `build_graph` feeds up to `knowledge_graph.max_findings` (200, `config.yaml:137`) findings to `anthropic/claude-opus-4.8` (`config.yaml:134`) — deliberately the most expensive model in the engine, because "the KG is the trust artifact". Paying that at ingest for a graph nobody may open is the wrong trade.
3. **The graph is read-only decoration.** Every retrieval path — `delapan_search` (`mcp/server.py:80`), `select_preamble` (`preamble.py:147`) — is pure `match_findings` cosine KNN. `kg_nodes` never influences a ranking.

Meanwhile `node_match_threshold` (`config.py:362`, `config.yaml:138`) is **dead config**: it is defined and never read anywhere in `delapan/` or `tests/`. Node identity is exact `(kb_id, type, label)` on *both* tiers (`sqlite.py:612-615`, `supabase.py:310-312`), and each caller passes its own `min_similarity` to `match_kg_nodes`. The `builder.py:12-16` docstring claiming the cloud tier dedupes via pgvector near-match is stale for the same reason — flag both for cleanup; neither is load-bearing here.

## Decisions (locked)

1. **Full end state, both halves.** Part A: lazy ingest (cheap NLP scaffold at ingest, LLM extraction deferred to view time, cached by finding-set hash). Part B: findings-as-nodes + PPR reranker.
2. **PPR ranks findings; it never replaces vector retrieval.** Vector search stays the only retrieval path — PPR reorders/prunes the candidate pool it is handed. Graph-replaces-vector degrades recall.
3. **Default OFF.** The enable gate is E1 golden-set recall@k non-regression.
4. **The rerank seam is E3's, not this spec's** (see § Sequencing).
5. **Rerank is not wired into the preamble.** `delapan_search` only.

## Design

Two graph tiers share `kg_nodes`/`kg_edges`, split by a new `tier` column:

- `nlp` — the scaffold: one node per finding, one node per extracted phrase. Machine-derived, regenerable, never shown by default.
- `llm` — the curated graph users see and hand-edit in the frontend. **Every existing row is `llm`** (see § Store changes: the column lands `NOT NULL DEFAULT 'llm'`, so the migration *is* the backfill — no `COALESCE`, no "legacy reads as llm" special case).

```
INGEST  (per explore — fire-and-forget, mirrors schedule_kg_update, mcp/server.py:121)
  finding rows ─► extract_phrases (RAKE-style, pure py) ─► concept nodes (tier=nlp, embedded)
               └► finding nodes  (tier=nlp, NOT embedded, label = finding id)
                    └─ contains ─► concepts ─ cooccurs ─ concepts

VIEW TIME  (GET /graph, routes_kg.py:60)
  scaffold_hash(active finding ids) ≠ kg_extraction_state.grounded_hash ?
    └► schedule build_graph(finding_ids=delta, rebuild=False)   # append-only; never clears
  serve tier=llm immediately, possibly stale — never blocks

QUERY TIME  (delapan_search, mcp/server.py:70; NOT preamble.py:147)
  match_findings(limit × pool_multiplier) ─► E3 Reranker seam ─► "ppr"
    embed(query) ─► match_kg_nodes(tier='nlp') seeds ─► 2-hop subgraph (tier='nlp')
      ─► power iteration ─► blend(sim_norm, ppr_norm) over finding nodes ─► top-limit
```

**Taxonomy — the BLOCKER resolution.** `tier` is a real column and is **part of the node dedupe key on both stores**; `type` semantics are unchanged. The scaffold uses exactly two types, `finding` and `concept`, and its `concept` nodes coexist with the LLM tier's own `type="concept"` (`extractor.py:31-32`) without collision because the key is `(kb_id, tier, type, label)`.

The rejected alternative — reserved scaffold types (`nlp_concept`/`nlp_finding`) — was chosen against for two concrete reasons:

- **Edges need a tier discriminator anyway.** Edge identity is `(kb_id, source, target, relation)` (`sqlite.py:753-757`); a naming convention on *node types* gives edges nothing, so `kg_edges` would get a `tier` column regardless. Reserved types buy asymmetry, not simplicity.
- **It leaks into the user-visible ontology.** `kg_stats` groups by `type` (`sqlite.py:981-985`), and `kg_schema` derives the KB's emergent ontology straight from `stats["by_type"]` (`service.py:71-79`), which `kg_schema_view` shows next to the user's approved intent so drift is visible. `nlp_concept` would appear as emergent drift in every KB unless every consumer learned the prefix. A `tier` column keeps `type` clean and makes the default filter one predicate.

**Node identity — the 1:1 fix.** Scaffold finding nodes are keyed on the **finding id, not the title**: `label = <finding_id>`, `type = "finding"`, `properties = {"title": …}`, `grounded_in = [finding_id]`. Titles repeat across re-explores; keying on them collapsed N findings into one node (`grounded_in` unioning both ids via `_merge_kg_node`, `sqlite.py:648-666`), which breaks the 1:1 mapping PPR maps mass back through. Keying on the id makes the map-back a direct `label → row` lookup with no `grounded_in` parsing. The label is never user-facing (tier `nlp` is hidden), so a uuid label costs nothing.

**Embedding split.** Only **concept** nodes are embedded. Finding nodes are not — their text already lives in `vec_findings`. This is free correctness, not just cost: `match_kg_nodes` joins `vec_kg_nodes` (`sqlite.py:793-795`) / requires `embedding is not null` (live RPC body), so seeds are *structurally* concepts only.

**Eager extraction is untouched.** The intent-schema gate stays exactly as-is — intent KBs keep their eager incremental auto-build (`builder.py:233-234`), and manual `build_graph` is unchanged. Lazy view-time extraction serves the intent-less majority. The NLP scaffold runs for **all** KBs; PPR needs it regardless of intent.

**NLP choice (dependency-honest).** RAKE-style stopword-delimited phrase extraction: split `title + content` on sentence/stopword boundaries, keep 1–`max_phrase_words` runs, normalize with the builder's `_norm` idiom (`builder.py:36-38`), rank by degree/frequency, take top-K. Pure python with an embedded ~175-word stopword frozenset. `nltk`/`spacy` are absent from `pyproject.toml` (verified) and a POS model would violate the no-heavy-deps rule. Scaffold phrases are retrieval plumbing, never rendered — RAKE quality suffices.

## Components

### C1 · `delapan/core/knowledge_graph/nlp_phrases.py` (new)

Pure, no IO. `extract_phrases(title: str, content: str, cfg: LazyKGConfig) -> list[str]` — deterministic, returns ≤ `cfg.max_phrases_per_finding` normalized phrases of ≤ `cfg.max_phrase_words` words. Module-level `_STOPWORDS: frozenset[str]`.

### C2 · `delapan/core/knowledge_graph/lazy.py` (new)

Scaffold ingest, lazy LLM build, and the staleness cache.

- `async ingest_scaffold(ctx, store, finding_rows: list[dict]) -> None` — phrases → one `embed_batch` over the distinct new phrases → `upsert_kg_nodes` (finding + concept nodes, `tier="nlp"`) → `upsert_kg_edges` (`contains`, `cooccurs`, `tier="nlp"`). `cooccurs` pairs are emitted in one canonical direction (endpoints sorted by label) so the store's `(source, target, relation)` dedupe (`sqlite.py:753-757`) makes re-ingest idempotent.
- `schedule_scaffold_update(ctx, finding_ids, *, store=None) -> None` — signature and strong-ref task-set pattern copied from `schedule_kg_update` (`builder.py:255-267`); hydrates bodies via `get_finding` per id (small: one explore's worth) and calls `ingest_scaffold`.
- `scaffold_hash(ids: list[str]) -> str` — delegates verbatim to `grounded_hash` (`concept_doc.py:32-40`), the FNV-1a 32-bit key already shared with the frontend.
- `async ensure_llm_graph(ctx, store) -> None` — enumerate active finding ids (§C5), compare `scaffold_hash(active)` against `kg_extraction_state.grounded_hash`; on mismatch schedule `build_graph(ctx, finding_ids=delta, rebuild=False, store=store)` where `delta = active − state.finding_ids`, capped at `knowledge_graph.max_findings` per pass (successive views converge). On success, advance the state row. **`rebuild=False` is load-bearing**: `build_graph(rebuild=True)` calls `clear_kg` (`builder.py:188`), which would destroy the frontend's hand-curated nodes. The lazy path never clears.
- `async backfill_scaffold(ctx, store) -> None` — enumerate ids (§C5), hydrate each body via `get_finding`, ingest in batches. Triggered once, from PPR, when a KB has zero `nlp` nodes. The per-id hydration is an accepted N+1 (one-time, background, per KB) — the same posture `list_projects` already documents (supabase-store spec §7); the Store exposes no bulk get-by-ids and this spec does not add one.

### C3 · `delapan/core/knowledge_graph/repair.py` (new) — re-derived against SOFT invalidation

The write-path spec **keeps** superseded rows (`invalidated_at` set, row retained) and calls this out as a KG-safety improvement over the branch's hard `DELETE`: `grounded_in` references stay resolvable, and `get_finding` still returns invalidated rows. **So there is no integrity repair to do.** Concretely:

- **No dangling references.** Concept `grounded_in` entries pointing at an invalidated finding still resolve. Stripping them would be pure ceremony — and `grounded_in` is capped at `_MAX_GROUNDED = 50` (`sqlite.py:138`, `supabase.py:21`), so it is lossy by construction and must never be used as a refcount.
- **No ranking bug.** An invalidated finding's scaffold node can still receive PPR mass, but `match_findings` now carries `AND invalidated_at IS NULL`, so that finding never appears in `rows` and its mass is discarded at map-back.
- **No LLM-tier repair.** Invalidation changes the active id set → `scaffold_hash` changes → the next view schedules a delta build. Self-healing, no hook.

What remains is a **precision hygiene pass over the derived scaffold only**:

`schedule_graph_repair(ctx, invalidated_ids: list[str], *, store=None) -> None` — for each invalidated id: read the `(tier='nlp', type='finding', label=<id>)` node's 1-hop neighbourhood, `delete_kg_node` it (which already cascades incident edges — `sqlite.py:707-731`), then delete any neighbour concept left with zero remaining `contains` edges. Hard-deleting here is consistent with soft invalidation, not a contradiction of it: tier `nlp` is machine-derived, never user-edited, and fully regenerable by `backfill_scaffold`. The pass **never touches tier `llm`** — which is what made the branch's repair dangerous, since `delete_kg_node` on a curated node is unrecoverable.

**Sourcing the ids** (per the write-path spec's shape): `affected_finding_ids` carries the **new** row's id for UPDATE/SUPERSEDE — the target id appears **only in the event log**. So the caller in `resolve_and_persist` derives:

```python
invalidated = [e.target_finding_id for e in outcome.events
               if e.op in ("UPDATE", "SUPERSEDE") and e.target_finding_id]
schedule_scaffold_update(ctx, outcome.affected_finding_ids, store=store)  # new/added rows
schedule_graph_repair(ctx, invalidated, store=store)                      # retired rows
```

No new Store methods are needed: `get_kg_subgraph(seed_node_ids=[...], depth=1)` + `delete_kg_node` + `get_kg_node` cover it. **Under the write-path spec's cloud pure-ADD guard there are no UPDATE/SUPERSEDE events on cloud**, so the hook is a verified no-op there until C6 lands.

### C4 · `delapan/core/retrieval/ppr.py` (new)

`class PPRReranker` implementing **E3's** `Reranker` protocol.

1. Seed: `await store.match_kg_nodes(kb_id, ctx.query_embedding, match_count=cfg.ppr_seed_count, min_similarity=cfg.ppr_seed_min_similarity, tier="nlp")`. Zero seeds → return `rows` unchanged.
2. Fetch: `store.get_kg_subgraph(kb_id, seed_node_ids=[…], depth=2, node_cap=cfg.subgraph_node_cap, edge_cap=cfg.subgraph_edge_cap, tier="nlp")`. The caps are passed **explicitly** — the existing `get_kg_subgraph` defaults are `node_cap=200, edge_cap=600` (`base.py:201-202`, `sqlite.py:822-823`). The 400/1200 defaults for the new knobs are *borrowed from* `knowledge_graph.max_nodes`/`max_edges` (`config.py:363-364`), which are per-build UPSERT safety caps, not traversal caps.
3. Walk: adjacency built **undirected** (the scaffold is bipartite-ish; `contains` must be traversable finding↔concept both ways or mass never returns). Personalization vector = seed similarities, L1-normalized. `r = (1−d)·p + d·Aᵀ·r` with column-normalized `A`, dangling mass returned to `p`; `cfg.ppr_iterations` iterations at `cfg.ppr_damping`. Bounded by the caps → trivial cost.
4. Map back: node `label` **is** the finding id for `type="finding"` nodes. `score = alpha·sim_norm + (1−alpha)·ppr_norm`, both min-max normalized over the pool (zero range → that term contributes 0.5). Rows with no scaffold node get `ppr_norm = 0`. Sort desc, annotate `rerank_score`, truncate to `ctx.limit`.

Seeding from the candidate pool's own finding nodes is deliberately excluded: it would re-inject vector order into the personalization vector and blunt the rerank.

### C5 · Enumeration — consumed, not defined

`ensure_llm_graph` / `backfill_scaffold` / `scaffold_hash` all need the full active id set, and **`list_findings` caps at `LIST_MAX_LIMIT = 100` on both tiers** (`sqlite.py:84,375`; `supabase.py:161-162`) — for the visualization KB at 361 findings, "all findings" via `list_findings` is a truncated recency window and every hash and delta is wrong.

**Sibling spec E6 (consolidation) OWNS the enumeration method**: `list_finding_digests(kb_id, *, limit=5000, include_invalidated=False) -> list[dict]` returning `id/title/category/confidence/created_at`. This spec **consumes** it and does not define a competing `list_finding_ids`. Whichever of E6/E7 lands first implements it **per E6's signature** on the Protocol + both stores; the second consumes it as-is. Its `include_invalidated=False` default is what makes the id set *active*.

### C6 · Call-site edits (three, all small)

- `mcp/server.py:80` — when a reranker is active, fetch `limit × cfg.lazy_kg.pool_multiplier` candidates and hand them to the seam. **This is the only search entry point in open-core**: there is no `/v1` surface here (`api/main.py:12-13` puts `/v1/*` behind the `[cloud]` extra; `main.py:38-42` mounts only health/projects/kg/findings/explore, and `routes_findings.py` has no search route at all). Any `/v1` wiring is a cloud-repo follow-up outside this codebase.
- `mcp/server.py:121` — add `schedule_scaffold_update(ctx, ids, store=store)` beside the existing `schedule_kg_update(ctx, ids, store=store)`.
- `routes_kg.py:60` — `get_graph` must become **`async def`**. Today it is a sync `def`, so FastAPI runs it in a threadpool worker with **no running event loop**, and the `asyncio.create_task` pattern this spec reuses from `builder.py:265` would raise `RuntimeError` there. Making it `async def` alone would move the store's sync reads (multiple blocking PostgREST round-trips on cloud) onto the loop — a regression. So: `async def` **plus** `await run_in_threadpool(...)` (`starlette.concurrency`) around `resolve_kb_or_404` and `read_graph`, then fire `ensure_llm_graph` on the now-running loop. Non-blocking property preserved, loop available.
- `builder.py:188` — `store.clear_kg(ctx.kb_id, tier="llm")`. Without this, a manual `build_graph(rebuild=True)` wipes the scaffold.

## Store changes

### Protocol (`store/base.py`)

- `tier: str | None = None` kwarg (None = all tiers, today's behavior) on `match_kg_nodes` (`:188`), `get_kg_subgraph` (`:198`), `list_kg_nodes` (`:214`), `clear_kg` (`:233`); `kg_stats` (`:228`) takes `tier: str = "llm"` and additionally returns an additive `scaffold: {node_count, edge_count}`.
- `upsert_kg_nodes` / `upsert_kg_edges` row dicts gain a `"tier"` key defaulting to `"llm"` — no signature change.
- **Node dedupe key becomes `(kb_id, tier, type, label)`** on both stores: the lookup `SELECT` gains `AND tier = ?` and the in-batch `batch` dict key widens from `(typ, label)` to `(tier, typ, label)` (`sqlite.py:588-645`; `supabase.py:298-328`). The merge paths (`_merge_kg_node` `sqlite.py:648-666`; `_merge_node` `supabase.py:330-341`) are **unchanged** — identity now includes tier, so a merge is always same-tier and can never flip a curated node's tier.
- On `kg_edges`, `tier` is a **read filter only, not part of edge identity**: cross-tier edge collision is impossible because endpoints are tier-partitioned node ids.
- New: `get_kg_extraction_state(kb_id) -> dict | None`, `set_kg_extraction_state(kb_id, *, org_id, grounded_hash, finding_ids, model) -> None`.
- **Read defaults** — `read_graph`/`kg_digest` (`service.py:49,61,137`) and `list_kg_nodes` pass `tier="llm"`; the visualizer and `kg_schema_view` are byte-compatible with today because every pre-existing row *is* `llm` after migration.

### SQLite (`store/sqlite.py`)

Base `_SCHEMA` (`:86`): `kg_nodes` and `kg_edges` CREATE TABLE each gain `tier TEXT NOT NULL DEFAULT 'llm'` (fresh DBs), and a new table is appended (safe — a new table has no ALTER dependency):

```sql
CREATE TABLE IF NOT EXISTS kg_extraction_state (
  kb_id TEXT PRIMARY KEY, org_id TEXT, grounded_hash TEXT,
  finding_ids TEXT, model TEXT, built_at TEXT);
```

`_ADD_COLUMN_MIGRATIONS` (`:130`) gains, **in this order**:

```python
"ALTER TABLE kg_nodes ADD COLUMN tier TEXT NOT NULL DEFAULT 'llm';",
"ALTER TABLE kg_edges ADD COLUMN tier TEXT NOT NULL DEFAULT 'llm';",
"CREATE INDEX IF NOT EXISTS idx_kg_nodes_kb_tier_dedupe ON kg_nodes(kb_id, tier, type, label);",
"CREATE INDEX IF NOT EXISTS idx_kg_edges_kb_tier ON kg_edges(kb_id, tier);",
```

**The two indexes must live here, not in `_SCHEMA`.** `_ensure_schema` runs `executescript(_SCHEMA)` — *not* wrapped in try/except — **before** the migration loop (`sqlite.py:192-196`). An index over `tier` inside `_SCHEMA` fails with `no such column: tier` on every existing DB, crashing `SQLiteStore.__init__` on every open. Each `_ADD_COLUMN_MIGRATIONS` statement runs in its own try/except, so the duplicate-column `ALTER` on a fresh DB is swallowed harmlessly and the index then succeeds on both paths.

SQLite permits `NOT NULL` on `ADD COLUMN` given a constant default, and pre-existing rows read back as `'llm'` without a table rewrite — **the migration is the backfill**. No `COALESCE` appears anywhere.

The tier predicate goes **inside the SQL WHERE**, never as a post-filter. `match_kg_nodes` applies `ORDER BY dist LIMIT ?` before filtering on `min_similarity` (`sqlite.py:792-801`); a post-filter would let `llm` nodes consume the whole KNN window and starve `nlp` seeding.

### Supabase migration (full, matching the live catalog)

```sql
-- 1. tier axis. Constant default → PG11+ fast default, no table rewrite; every
--    existing row reads 'llm'. This IS the legacy migration.
alter table kg_nodes add column if not exists tier text not null default 'llm';
alter table kg_edges add column if not exists tier text not null default 'llm';
create index if not exists idx_kg_nodes_kb_tier_dedupe on kg_nodes (kb_id, tier, type, label);
create index if not exists idx_kg_edges_kb_tier on kg_edges (kb_id, tier);

-- 2. lazy-build state
create table if not exists kg_extraction_state (
  kb_id uuid primary key references kbs(id) on delete cascade,
  org_id uuid not null,
  grounded_hash text not null,
  finding_ids jsonb not null default '[]',
  model text,
  built_at timestamptz not null default now()
);
alter table kg_extraction_state enable row level security;

-- Policy shape copied VERBATIM from the live kg_nodes policies: membership via
-- auth.uid(). There is no org_id JWT claim in this auth model — an
-- `auth.jwt() ->> 'org_id'` policy would satisfy neither USING nor WITH CHECK
-- and leave the table deny-all (the feature would silently never work on cloud).
create policy kg_extraction_state_select on kg_extraction_state for select
  using (org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid()));
create policy kg_extraction_state_insert on kg_extraction_state for insert
  with check (org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid()));
create policy kg_extraction_state_update on kg_extraction_state for update
  using (org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid()));
create policy kg_extraction_state_delete on kg_extraction_state for delete
  using (org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid()));

-- 3. match_kg_nodes: DROP then CREATE — never `create or replace` with an added
--    parameter. That makes an OVERLOAD, and PostgREST then cannot disambiguate
--    the existing 4-key call (supabase.py:285-286) — "could not choose the best
--    candidate function". Body + returns-table below are verbatim from
--    pg_get_functiondef on the live instance (2026-07-16) plus the tier
--    predicate; the returns-table shape is UNCHANGED (the Store contract at
--    base.py:188-195 and PPR seeding both consume `similarity`).
--    After the drop+create the 4-key call shape keeps working via the default.
drop function if exists match_kg_nodes(vector, uuid, integer, real);
create function match_kg_nodes(
  query_embedding vector,
  match_kb_id uuid,
  match_count integer default 1,
  min_similarity real default 0.0,
  match_tier text default null
)
returns table(id uuid, type text, label text, properties jsonb, similarity real)
language sql
stable
as $function$
  select
    n.id, n.type, n.label, n.properties,
    1 - (n.embedding <=> query_embedding) as similarity
  from kg_nodes n
  where n.kb_id = match_kb_id
    and n.embedding is not null
    and (match_tier is null or n.tier = match_tier)
    and 1 - (n.embedding <=> query_embedding) >= min_similarity
  order by n.embedding <=> query_embedding
  limit match_count;
$function$;
```

The drop+create is a brief window in which in-flight 4-key calls fail; apply it during the same maintenance step as the write-path spec's `match_findings` change.

### Frontend contract (`delapan-ai/frontend`)

`buildGraph` clears and rebuilds from `GraphResponse` (`src/graph/build.ts:11`), so `nlp` nodes leaking into `GET /graph` would render — the `tier="llm"` default in `read_graph` is what prevents it, and no frontend change is required for it. The one additive change: `kg_stats`'s new `scaffold` key needs `scaffold?: { node_count: number; edge_count: number }` on `GraphStats` (`src/api/types.ts:53-58`) **and** a matching emission from `mock.ts`'s `getStats` (`src/api/mock.ts:721-727`) — the repo's mock-parity rule (`frontend/CLAUDE.md:50`) requires both.

## Config

`LazyKGConfig` in `config.py` + `lazy_kg:` in `config.yaml`, registered on `AppConfig` (`config.py:400-416`), overridable as `DLP_LAZY_KG__<FIELD>` via the existing `_env_overrides` walker:

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | scaffold ingest on/off (PPR is gated separately by the reranker knob) |
| `max_phrases_per_finding` | `8` | RAKE top-K per finding |
| `max_phrase_words` | `4` | longest phrase run kept |
| `ppr_damping` | `0.85` | power-iteration damping |
| `ppr_iterations` | `20` | fixed iteration count (no convergence check needed at these caps) |
| `ppr_alpha` | `0.5` | blend weight: `alpha·sim_norm + (1−alpha)·ppr_norm` |
| `ppr_seed_count` | `8` | `match_kg_nodes` seeds |
| `ppr_seed_min_similarity` | `0.5` | seed floor |
| `pool_multiplier` | `3` | candidate over-fetch depth: `pool = limit × this` |
| `subgraph_node_cap` | `400` | PPR traversal cap (borrowed from `knowledge_graph.max_nodes`) |
| `subgraph_edge_cap` | `1200` | PPR traversal cap (borrowed from `knowledge_graph.max_edges`) |

`pool_multiplier` lives here rather than with the seam because over-fetch depth is a PPR-tuned quantity; the seam reads it only when a reranker is active.

**The one seam knob is E3's**: `retrieval.reranker: "none" | "llm" | "ppr"` in E3's `RetrievalConfig`. This spec adds **no** `search.reranker` and no second selector.

## Error handling

- Phrase extraction / scaffold ingest fails → logged, explore result unaffected. Same never-raises contract as `_auto_kg_update` (`builder.py:251-252`).
- Reranker raises → caller logs and keeps vector order (E3's seam contract).
- Zero seeds, or seeds below `ppr_seed_min_similarity` → PPR returns `rows` unchanged.
- Zero `nlp` nodes in the KB → PPR returns `rows` unchanged **and** schedules `backfill_scaffold` once.
- Lazy LLM build fails → `kg_extraction_state` is **not** advanced; the stale `llm` graph keeps serving; the next view retries.
- `set_kg_extraction_state` fails → logged, not raised; worst case a redundant delta build next view.
- `repair` on a missing node → `delete_kg_node` already returns `{"deleted": False}` without touching anything (`sqlite.py:713-717`).
- Extraction with `use_json_schema=False` already returns empty-on-failure (`extractor.py`) — unchanged.

## Testing & acceptance criteria

1. `extract_phrases` is deterministic across runs, honors `max_phrase_words` and `max_phrases_per_finding`, and emits no stopword-only phrases.
2. Power iteration matches a hand-computed 5-node fixture to 1e-6, and its output is invariant to input node order.
3. **Node identity:** two findings with the **same title** produce **two** distinct `(tier='nlp', type='finding')` nodes; a scaffold `concept` node and an LLM-tier `concept` node with the **same label** coexist as two rows and neither merges into the other; the LLM node's `tier` is unchanged after a scaffold upsert. Both stores (`SQLiteStore` + fake-supabase, in the style of `tests/test_supabase_kg_write.py`).
4. **Idempotence:** re-ingesting the same finding rows creates zero new nodes and zero new edges (both stores).
5. **Migration:** opening an **existing** SQLite DB (pre-tier `kg_nodes`) succeeds and every pre-existing row reads `tier='llm'`; opening a fresh DB succeeds; opening twice succeeds. This is the regression test for the `_SCHEMA`-vs-`_ADD_COLUMN_MIGRATIONS` ordering trap.
6. **Default reads are byte-compatible:** with a scaffold present, `read_graph`, `kg_stats["node_count"]`, `kg_digest`, and `kg_schema_view["emergent"]` return exactly what they returned before the scaffold existed. `kg_stats["scaffold"]["node_count"]` is non-zero.
7. **`clear_kg` scoping:** `build_graph(rebuild=True)` leaves the `nlp` node/edge counts unchanged.
8. **Repair:** given a SUPERSEDE event, the invalidated finding's `nlp` finding-node and its `contains` edges are gone; a concept still grounded by a surviving finding **survives**; zero `tier='llm'` rows are touched; the surviving finding's own scaffold node is intact.
9. **`match_kg_nodes` tier filter is server-side:** seeding a KB whose top-`match_count` cosine neighbours are all `llm` nodes still returns `nlp` seeds (proves the predicate precedes the `LIMIT`, not a post-filter). Both stores.
10. **Cloud parity:** the fake-supabase suite asserts the 5-key RPC params and the `(kb_id, tier, type, label)` dedupe select; the env-gated live smoke (`RUN_CLOUD_TESTS=1`) asserts the 4-key legacy call still works post-migration via the default.
11. **The enable gate (E1):** run each per-KB golden query set twice, `reranker=none` vs `reranker=ppr`. **Recall@k non-regression is the pass condition**; report MRR and nDCG deltas. `retrieval.reranker` flips to `"ppr"` by default only if recall@k does not regress on any set.
12. Preamble/band calibration is untouched **by construction** — no reranker is wired into `preamble.py:147`, so bands stay raw cosine. Asserted by the existing E1 preamble tests staying green unmodified.

## Sequencing & dependencies

**Part A (scaffold + lazy build) is standalone on master.** It needs nothing from E2/E3/E6 except the §C5 enumeration method. Order: tier column + dedupe key (both stores) → `nlp_phrases` → `lazy.py` → the four call-site edits → migration.

**Part B (PPR) lands after E3's seam.** E3 (hybrid retrieval) **owns** `delapan/core/retrieval/rerank.py`, the `Reranker` protocol, the `RerankContext` dataclass, and the `get_reranker(name)` registry:

```python
class Reranker(Protocol):
    async def rerank(self, query: str, rows: list[dict], *, ctx: RerankContext) -> list[dict]: ...

@dataclass
class RerankContext:
    store: Store; kb_id: str; limit: int; query_embedding: list[float] | None
```

Contract: reorder/drop only, never fabricate, `≤ ctx.limit` rows out, optional `rerank_score` annotation; any exception → the caller keeps vector order. **This spec does not create that file.** It implements `retrieval/ppr.py` and **registers `"ppr"` into E3's registry** via the entry point E3 exposes for exactly this: `register_reranker(name, impl)`, called at `ppr.py` import time, with `delapan/core/retrieval/__init__.py` importing both built-in modules so `get_reranker` sees them. Coordinate on that name and on `RerankContext`'s fields; nothing else.

**E2 (write path):** `schedule_graph_repair`'s only caller is `resolve_and_persist`. If E2 has not landed there are no UPDATE/SUPERSEDE events and the hook has no caller — the scaffold stays append-only and correct. On cloud, E2's pure-ADD guard means the hook is a no-op until E2's C6.

**E6 (consolidation):** owns `list_finding_digests`. Whichever of E6/E7 lands first implements it per E6's signature; the other consumes it.

**Rollout:** (1) tier column + dedupe key + migration, `lazy_kg.enabled=false`; (2) flip `lazy_kg.enabled=true` — scaffold ingest starts, PPR still unreachable (`retrieval.reranker` defaults `"none"`, and Part B may not exist yet); (3) lazy view-time build; (4) Part B after E3's seam, still default-off; (5) flip the default only on acceptance #11.

## Open risks

1. **RAKE phrase noise on technical corpora** inflates hub concepts and washes out the walk (every finding connects to "knowledge base"). *Resolution:* `retrieval.reranker` stays `"none"`; the flip is gated on #11's golden-set deltas, so a bad scaffold is measured, not shipped. `max_phrases_per_finding=8` and the `ppr_seed_min_similarity` floor bound the damage; if #11 fails, IDF-weighting the concept nodes is the first lever.
2. **Unweighted co-occurrence edges.** Both stores skip duplicate edges (`sqlite.py:748-755`, `supabase.py:355-359`), so a repeated pair cannot accumulate a count without a new merge path — the walk cannot distinguish a pair seen once from one seen fifty times. *Resolution:* ship unweighted; add weight-increment to `upsert_kg_edges` only if #11 shows ranking is seed-insensitive. Recorded so the null result is interpretable rather than mysterious.
3. **`kg_extraction_state.finding_ids` grows with the KB** (~180KB of JSON at E6's `limit=5000`). *Resolution:* accept — it is one row per KB, read once per view, and 5000 is the enumeration ceiling by construction. If a KB exceeds it, the delta degrades to "the most recent 5000", which the per-pass `max_findings` cap already bounds; the graph stays append-only and correct, just incomplete at the tail.

## Out of scope

Weighted/typed scaffold edges; community detection over the `nlp` tier; exposing the scaffold in the visualizer; `/v1` rerank wiring (no such surface in open-core — cloud-repo follow-up); replacing vector retrieval with graph traversal (verified to degrade recall); rerank in the preamble (bands stay cosine-pure by decision).
