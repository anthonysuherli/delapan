# Hybrid retrieval: contextual embeddings, BM25+RRF fusion in-store, pluggable rerank

*2026-07-16 · Status: approved design, pre-implementation · Line: master (via `feat/write-path-dedup`) · Portfolio item E3 from `delapan-ai/reports/2026-07-15-delapan-enhancement-brainstorm.md` · Adversarially verified against the codebase and the live cloud instance `<project-ref>`; all findings folded in.*

## Problem

Retrieval is single-channel cosine KNN. SQLite brute-forces `vec_distance_cosine` over a join (`store/sqlite.py:208-259`, query at `:241`); Supabase delegates to the deployed `match_findings` RPC (`store/supabase.py:101-114`). Embeddings are context-free: `resolve_and_persist` embeds the rendered body alone (`core/memory/persist.py:68-69`), so a finding titled "Discount rate" carries no signal about *which* KB or category it belongs to. A lexically exact query ("IFRS 17 CSM") has no channel that rewards exact term overlap, and banding (`core/agent/preamble.py:38-51`) inherits every recall miss — the W4 failure (an off-topic-but-`rich` resume) is partly a recall failure, not only a threshold failure.

External grounding (brainstorm §2, adversarially verified): Anthropic's **contextual-retrieval ladder** — (1) prepend 50–100 tokens of context before embedding → top-20 retrieval failure **−35%**; (2) add BM25 hybrid → **−49%**; (3) add a reranker → **−67%**. Each rung is independently measurable. Supabase's canonical hybrid pattern (tsvector CTE + pgvector CTE + Reciprocal Rank Fusion, knobs `full_text_weight`/`semantic_weight`/`rrf_k=50`) makes rung 2 a one-SQL-function upgrade.

## Decisions (approved 2026-07-16)

1. **All three rungs**, in rung order. Rung 2 first (no distribution shift), then rung 3 (inert until flipped), then rung 1 + backfill paired with an E1 recalibration.
2. **Cosine similarity remains the banding/coverage signal.** Fusion *selects* candidates; rerank *orders* them. See §Banding.
3. **Rerank is not on the preamble path.** `select_preamble`/`render_preamble` are untouched; rerank applies to `delapan_search` only.
4. **This spec owns the rerank seam** (`core/retrieval/rerank.py`, the `Reranker` protocol, `RerankContext`, the registry). E7 registers a `"ppr"` implementation into it.
5. **One knob for the seam**: `retrieval.reranker`. E7 does **not** add `search.reranker` — there is exactly one knob, defined in §Config.
6. **The resolver stays embedding-only.** E2's approved spec scopes it that way by design (§Out of scope there); E3 does not touch `resolver.py`.
7. Rung-1 backfill: forward-path change + a standalone script, dry-run default, `--apply` to execute.

**Substrate**: the E1+E2 write-path spec (`2026-07-16-write-path-dedup-design.md`) is the authority. Its vocabulary — `ResolutionOp = ADD|UPDATE|NOOP|SUPERSEDE`, `valid_from`/`invalidated_at`/`superseded_by`, the `supersede_finding`/`invalidate_finding`/`update_finding(None→keep)` primitives, `match_findings … AND invalidated_at IS NULL`, C6's `SupabaseStore` parity, the cloud pure-ADD guard — is assumed, not redefined here. `feat/mem0-resolution-port` is already merged into `feat/write-path-dedup` (`668f61e`), so `resolve_and_persist` is live as the single persist path.

## Banding — why cosine stays the signal

`band1_min=0.55 / band2_min=0.40 / band3_min=0.25` (`config.yaml:38-40`, `config.py:195-197`) are **absolute cosine thresholds** driving the `rich`/`sparse`/`gap` verdict that gates explore-auto. RRF scores (~`1/(50+rank)`, empirically 0.017–0.039 in the live rehearsal below) are **rank-relative**: they shift with pool size, so banding on them would make coverage verdicts KB-size-dependent — a 30-finding KB and a 3000-finding KB would band identically at rank 1. Therefore:

- Every fused row carries a true cosine `similarity`, `COALESCE`d to `0.0` (§Store changes) so `band_findings`' `r.get("similarity", 0.0)` sort (`preamble.py:42`) can never compare `None` to a float.
- Fusion changes **which** rows reach `band_findings`, never how they band.
- `render_preamble` keeps sorting by `similarity` (`preamble.py:85`) — unchanged.
- Thresholds need recalibration only because of **rung 1's** embedding-space shift. E1's `scripts/calibrate_bands.py` re-fits them; rungs 2 and 3 leave band semantics untouched.

## Design

One new front door wraps the Store seam. Fusion runs in SQL on both tiers (one statement / one RPC); rerank is a post-store, in-process stage on the search path only.

```
persist path (rung 1) — inside resolve_and_persist, the single persist site:
  candidates ─► render_content ─► contextual_text(category | kb topics | source domain + body)
                                       └─► embed_batch ─► rows (raw body stored; prefix is embed-only)

query path (rungs 2+3):
  query ─► embed_text ─┬─► vec channel (rank by cosine) ─┐
           query text ─┴─► fts channel (rank by BM25)  ─┴─► RRF fuse, in-store, one stmt/RPC
                                                             │  rows: match_findings shape
                                                             │       + similarity + rrf_score
                     delapan_search ────────────────────────►├─► rerank (none|llm|ppr) ─► rows
                     select_preamble ───────────────────────►└─► band_findings (cosine) ─► preamble
                                                                 (NO rerank — always-on path)
```

The context prefix is **embed-time only**: `findings.content` still stores the raw rendered body, so BM25, the resolver's neighbor bodies, and the OKF reader see unmodified text.

## Components

### C1 · `delapan/core/retrieval/context.py` (new) — rung 1

Pure helper, no IO.

```python
def contextual_text(
    *, title: str, body: str, category: str | None,
    topics: list[str], source_domain: str | None, cfg: RetrievalConfig,
) -> str:
```

Builds `category: <cat> | kb: <topic1>; <topic2> | source: <domain>\n\n<title>\n<body>`, with the prefix (everything before the blank line) truncated to `cfg.context_prefix_max_chars` (~100 tokens, per Anthropic's rung-1 recipe). Returns `f"{title}\n{body}"` unchanged when `cfg.context_prefix_enabled` is false. Topics come from the KB synopsis entries' `topic` key (the same shape `render_preamble` reads at `preamble.py:77`); `source_domain` is the registrable domain of the first provenance `url` (provenance is `[{url, query, accessed_at}]`, `core/exploration/render.py:49-59`).

### C2 · Rung-1 forward path

**With E2 (the assumed substrate — merged at `668f61e`): exactly one edit.** `resolve_and_persist` (`core/memory/persist.py:58-75`) computes `contents = [render_content(f.content) for f in candidates]` then `embeddings = await embed_batch(contents)` at `:68-69`. Insert one step between them: load the synopsis once per call (`load_synopsis(store, ctx.kb_id)`, `core/agent/synopsis.py:52`), derive `topics`, and embed `contextual_text(...)` per candidate while `_row_from_candidate` (`:28-41`) keeps writing the raw `render_content` body. Both call sites — `mcp/server.py:111` and `api/routes_explore.py:87` — route through this function, so neither is touched.

**Without E2 (bare master): the same edit lands twice**, in the `embed_batch(contents)` blocks of `mcp/server.py` and `api/routes_explore.py`, which master still carries. (The branch's DRY refactor `6731774` deletes both and removes the `embed_batch` imports — verified: `embed_batch` no longer appears in either file on `feat/write-path-dedup`.) There is no third site; the draft's "three persist sites" framing was wrong — they never coexist.

### C3 · `delapan/core/retrieval/hybrid.py` (new) — the front door

```python
async def retrieve(
    query: str, *, store: Store, kb_id: str | None, match_count: int,
    min_similarity: float, categories: list[str] | None = None,
    rerank: bool = False, cfg: RetrievalConfig | None = None,
) -> list[dict]:
```

`embed_text(query)` → `store.hybrid_match_findings(...)` → optional rerank → rows. Falls back to `store.match_findings(...)` when `cfg.hybrid_enabled` is false **or** the hybrid call raises (§Error handling), synthesizing `rrf_score = 1/(rrf_k + rank)` from the returned cosine order so row shape is stable for callers.

### C4 · `delapan/core/retrieval/rerank.py` (new) — rung 3, the owned seam

This module is the **public contract** for every reranker in the engine, LLM or graph-walk. E7's lazy-KG PPR walker registers into this registry; it adds no config knob and no second seam.

```python
@dataclass
class RerankContext:
    store: Store
    kb_id: str
    limit: int
    query_embedding: list[float] | None

class Reranker(Protocol):
    async def rerank(self, query: str, rows: list[dict], *, ctx: RerankContext) -> list[dict]: ...

def register_reranker(name: str, impl: Reranker) -> None: ...
def get_reranker(name: str) -> Reranker | None: ...   # "none" / unknown -> None
```

**Contract (binding on every implementation):**
- **Input** `rows` are `match_findings`-shaped dicts carrying `similarity` (and, from the hybrid path, `rrf_score`).
- **Output** is a subset of *those same row objects*, reordered and/or dropped, length ≤ `ctx.limit`. **Never fabricate, synthesize, or mutate a row's identity or content.** A reranker may annotate `rerank_score`.
- **Failure is not fatal**: any exception (or timeout) → the caller logs and keeps the incoming order. Rerankers do not need internal try/except for this.
- `ctx` carries `store` + `kb_id` + `query_embedding` precisely so a graph-walk reranker (E7's `"ppr"`) can hydrate a subgraph without a second retrieval seam.

Ships with one registered implementation, `"llm"`: a single `structured_completion` call (`core/clients/ai_gateway.py:63-113`, which already retries once on `fallback_model`) scoring the top-`rerank_top_n` rows 0–1 by id, wrapped in `asyncio.wait_for(cfg.rerank_timeout_s)`. `"none"` is not an entry — `get_reranker("none")` returns `None`, and `retrieve` skips the stage. No provider zoo.

### C5 · Caller switches

| Call site | Change |
|---|---|
| `delapan_search` (`mcp/server.py:70-81`) | `store.match_findings(...)` → `retrieve(..., rerank=True)`. **The only reranked path.** |
| `select_preamble` (`preamble.py:131-157`, store call at `:147`) | `store.match_findings(...)` → `retrieve(..., rerank=False)`. Keeps passing `cfg.band3_min` as `min_similarity` and `search.max_limit` as `match_count`. `render_preamble` untouched. |
| `resolver.py:74-78` | **Unchanged.** E2's approved spec scopes the resolver's candidate stage embedding-only by design; it depends on `min_similarity=cfg.neighbor_min_similarity` (0.6) for two behaviors — the zero-neighbor short-circuit that skips the LLM (`resolver.py:89`) and the hallucination guard restricting targets to returned neighbor ids (`resolver.py:122-123`). Revisiting is a future item, not E3. |
| `match_kg_nodes` (threshold 0.86, `config.py:362`) | **Unchanged.** Absolute-threshold dedupe over short entity labels gains nothing from rank fusion. |

### C6 · `scripts/reembed_findings.py` (new) — rung-1 backfill

Dry-run by default. Per KB: enumerate finding ids, `get_finding(kb_id, id)` to hydrate the body, rebuild `contextual_text`, re-embed in batches of 64 via `embed_batch`, and write with **E2's existing `update_finding` primitive** — no new write method:

```python
await store.update_finding(kb_id, fid, content=None, confidence=f["confidence"],
                           provenance=f["provenance"], embedding=new_vec, title=None)
```

`content=None` means *keep* under E2's None→keep contract; `confidence`/`provenance` are read back and passed verbatim (E2 defines None→keep for `content`/`embedding`/`title` only, so this touches no contract). Without E2's amendment, `SQLiteStore.update_finding` (`sqlite.py:310-341`) NULLs the body on `content=None` — so **the script requires E2's None→keep fix**, or must pass the body back verbatim. It states this dependency at startup and refuses `--apply` if unmet.

Dry-run prints per-KB counts, sample prefixes, and pre/post similarity stats on E1's golden queries. `--apply` re-embeds; re-runs are idempotent (the write overwrites).

**Enumeration — declared dependency on E6.** `list_findings` caps at `LIST_MAX_LIMIT = 100` on **both** tiers and takes **no offset** (verified: `sqlite.py:368-375`, `supabase.py:161-172` — signature `(kb_id, category=None, limit=None)`). Offset paging is therefore impossible without changing it. Per the cross-item resolution this script **depends on E6's `list_finding_digests(kb_id, *, limit=5000, include_invalidated=False)`** and invents no competing method. That digest returns `id, title, category, confidence, created_at` and deliberately **no content/provenance**, so the hydrate step is required, not optional: enumerate ids → `get_finding(kb_id, id)` per row (exists on both tiers: `sqlite.py:344`, `supabase.py:146`). On cloud that is one PostgREST round-trip per finding (~2.9k live today) — slow but acceptable for a one-shot, dry-run-gated script, and it keeps the digest contract E6 and E7 share unpolluted by E3's needs.

**Sequencing consequence, stated plainly:** rungs 1–3 all ship without this script — the forward path needs no enumeration. Only the *backfill of legacy rows* waits on `list_finding_digests`, so either E6's method is pulled forward as a shared prerequisite or the backfill runs at E6 time. §Open risk 1 covers the interim mixed-space window.

## Store changes

### Protocol (`delapan/store/base.py`) — exactly one new method

```python
async def hybrid_match_findings(
    self, kb_id: str | None, query_embedding: list[float], query_text: str,
    match_count: int, min_similarity: float, categories: list[str] | None = None,
    *, full_text_weight: float = 1.0, semantic_weight: float = 1.0, rrf_k: int = 50,
) -> list[dict]:
    """RRF-fused rows: match_findings shape + `rrf_score`. `similarity` is the true
    cosine, COALESCEd to 0.0 for rows with no embedding. `min_similarity` floors the
    final fused rows on that cosine — same semantics as match_findings."""
```

`min_similarity` is deliberately kept and applied to the **final** rows' cosine (not per-channel): it preserves `match_findings`' contract exactly, and it is what `select_preamble` passes `band3_min` into. Rows below `band3_min` would be dropped by `band_findings` anyway, so flooring them in-store costs no recall the preamble could have used, while an FTS-only hit *above* the floor that cosine ranked past `match_count` is exactly the recall rung 2 buys.

No `update_finding_embedding` and no `list_findings_full`: C6 reuses `update_finding` and E6's `list_finding_digests`.

### SQLite (`delapan/store/sqlite.py`)

- **`_SCHEMA`** (`:86`) gains, beside `vec_findings` (`:96`):
  `CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(title, content, finding_id UNINDEXED);`
- **FTS maintenance — all three write paths**, mirroring the `vec_findings` handling:
  - `insert_findings` (`:268`) — insert the fts row alongside the vec row.
  - `delete_finding` (`:404`) — delete the fts row alongside the vec row.
  - **`update_finding` (`:310`) — DELETE+INSERT the fts row.** Non-negotiable: E2 routes every resolution UPDATE through it, and without this the BM25 channel would rank superseded text while the vector channel ranks the new embedding. Supabase is immune (generated column).
- **Backfill in `_ensure_schema` (`:192`) — in Python, not SQL.** Page `SELECT id,title,content FROM findings WHERE id NOT IN (SELECT finding_id FROM findings_fts)`, render each body through the shared `render_content` (`core/exploration/render.py:18`) — legacy `content` may be a JSON dict (`sqlite.py:63-77`) and **pure SQL cannot invoke `_json_load` (`:1159`) or `render_content`**, so an `INSERT…SELECT` would index raw JSON braces and keys as BM25 tokens. The `NOT IN` guard keeps it idempotent; after the first pass it is a cheap no-op scan.
- **Query** — one statement. `vec_c` ranks the existing brute-force join (unlimited; it scans regardless); `fts_c` ranks `findings_fts MATCH ? ORDER BY bm25(findings_fts)` (**ascending — FTS5's bm25 is negative, more negative = better; do not add DESC**) over an OR-joined, token-quoted sanitized query, limited to `max(match_count,30)*2`; `FULL OUTER JOIN` on id; `rrf_score = ftw/(k+fts_rank) + sw/(k+vec_rank)` with a missing rank contributing 0 via COALESCE; `similarity = COALESCE(1.0 - vec_c.dist, 0.0)`. No `status` column exists locally, so that predicate is cloud-only (as today).
- **Floor**: `FULL OUTER JOIN` requires SQLite ≥ 3.39 (2022-06); FTS5, `bm25()`, and `row_number()` are likewise required. Verified in-repo end-to-end with `sqlite_vec` loaded: venv SQLite **3.53.1**, system 3.51.0 — both satisfy it. `_ensure_schema` raises a clear error on an older library rather than silently degrading.

### Supabase — full migration SQL

Cloud tables/RPCs live outside this repo; the change is not complete until applied. Types below **mirror the deployed `match_findings` exactly**, dumped live: `match_findings(query_embedding vector, match_kb_id uuid, match_count integer DEFAULT 10, min_similarity real DEFAULT 0.0) RETURNS TABLE(id uuid, title text, content text, category text, confidence real, tags text[], provenance jsonb, similarity real)`, `LANGUAGE sql STABLE`, body filtering `f.kb_id = match_kb_id AND f.embedding IS NOT NULL AND f.status <> 'discarded' AND 1-(…) >= min_similarity`. `findings.id`/`kb_id` are **uuid**, `confidence` **real (float4)**, `tags` **text[]**, `provenance` **jsonb** (live `information_schema.columns`). SQL-language functions are type-checked at CREATE time — `kb_id = <text>` has no operator and `id text`/`confidence float`/`tags jsonb` all fail to apply.

```sql
-- 00XX_hybrid_match_findings.sql
alter table findings add column if not exists fts tsvector
  generated always as (to_tsvector('english',
    coalesce(title,'') || ' ' || coalesce(content,''))) stored;
create index if not exists idx_findings_fts on findings using gin(fts);

create or replace function hybrid_match_findings(
  query_embedding vector,
  query_text text,
  match_kb_id uuid,
  match_count int default 10,
  min_similarity real default 0.0,
  full_text_weight float default 1.0,
  semantic_weight float default 1.0,
  rrf_k int default 50,
  categories text[] default null
) returns table (
  id uuid, title text, content text, category text, confidence real,
  tags text[], provenance jsonb, similarity real, rrf_score real
) language sql stable as $$
with fts_c as (
  select f.id, row_number() over (
      order by ts_rank_cd(f.fts, websearch_to_tsquery('english', query_text)) desc) as r
  from findings f
  where f.kb_id = match_kb_id
    and f.status <> 'discarded'
    -- E2 merge point: and f.invalidated_at is null
    and (categories is null or f.category = any(categories))
    and f.fts @@ websearch_to_tsquery('english', query_text)
  limit greatest(match_count, 30) * 2),
vec_c as (
  select f.id, row_number() over (order by f.embedding <=> query_embedding) as r
  from findings f
  where f.kb_id = match_kb_id
    and f.status <> 'discarded'
    -- E2 merge point: and f.invalidated_at is null
    and (categories is null or f.category = any(categories))
    and f.embedding is not null
  order by f.embedding <=> query_embedding
  limit greatest(match_count, 30) * 2)
select f.id, f.title, f.content, f.category, f.confidence, f.tags, f.provenance,
       coalesce(1 - (f.embedding <=> query_embedding), 0.0)::real as similarity,
       (coalesce(1.0/(rrf_k + fts_c.r), 0.0) * full_text_weight
      + coalesce(1.0/(rrf_k + vec_c.r), 0.0) * semantic_weight)::real as rrf_score
from fts_c full outer join vec_c on fts_c.id = vec_c.id
join findings f on f.id = coalesce(fts_c.id, vec_c.id)
where coalesce(1 - (f.embedding <=> query_embedding), 0.0) >= min_similarity
order by rrf_score desc
limit match_count;
$$;
```

**Predicate parity is load-bearing.** `status <> 'discarded'` appears in **both** CTEs: the deployed cosine RPC filters it, `SupabaseStore.insert_findings` writes `status="approved"` (`supabase.py:129`), and the live instance holds 5 `discarded` + 80 `pending` rows against 2839 `approved` — omitting it would silently resurface discarded findings on the hybrid path only. SQLite has no `status` column, so the predicate stays cloud-only.

**Verified live, read-only, no DDL** (inline `to_tsvector` standing in for the generated column, on PG 17.6): the statement type-checks and runs, and RRF demonstrably reorders — rows scoring on both channels (`fts_rank=1, vec_rank=2` → `rrf_score 0.0388`) outrank the pure-vector #1 (`similarity 1.0, rrf_score 0.0196`).

**Security**: declared without `security definer`, matching the deployed RPC — the function runs as invoker, so RLS stays authoritative. Live policies on `findings` are membership-based, not `org_id`-claim-based: `org_id IN (SELECT org_id FROM org_members WHERE user_id = auth.uid())` for select/insert/update/delete. `SupabaseStore.hybrid_match_findings` delegates via `rpc(...)` under `asyncio.to_thread`, exactly like `supabase.py:101-114`.

**Merge order with E2 — both specs touch retrieval SQL.** E2's migration `create or replace`s the deployed `match_findings` to add `and f.invalidated_at is null`. Whichever lands second owns reconciliation:
1. **E2 first (expected):** apply E2's migration, then apply this one with both `-- E2 merge point` comments replaced by the real predicate. One function, correct on arrival.
2. **E3 first:** apply this migration verbatim (the column does not exist yet, so the predicate must be absent or it fails to apply); E2's migration then `create or replace`s **both** `match_findings` **and** `hybrid_match_findings`, adding the predicate to all three CTEs/WHERE clauses. This is an explicit acceptance item for whichever PR lands second, and is why the merge points are marked in-line.

**Known pre-existing drift, not introduced here:** the deployed `match_findings` takes **4 params, no `categories`**, and does `where f.kb_id = match_kb_id` with **no `kb_id IS NULL` handling** — while `SupabaseStore.match_findings` forwards `categories` when given (`supabase.py:109-110`). A cloud fallback carrying `categories` would hit a PostgREST *function-signature-not-found* — i.e. the fallback itself errors. `hybrid_match_findings` therefore supports `categories` natively, and **the cloud fallback path drops `categories` and filters client-side** (§Error handling). `kb_id=None` (org-wide search) remains cloud-unsupported on both RPCs — unchanged, out of scope, noted so the degradation story does not silently depend on it.

## Config

New pydantic section `RetrievalConfig` in `core/config.py` + a `retrieval:` block in `config.yaml`, registered on `AppConfig`, overridable as `DLP_RETRIEVAL__<FIELD>` (`config.py:19-20`, `_ENV_PREFIX="DLP_"`, `_NESTED_DELIM="__"`). Nothing hardcoded. A new section — not the brainstorm's suggested `TiersConfig` knobs — keeps `tiers` meaning *banding* only.

| Field | Default | Meaning |
|---|---|---|
| `hybrid_enabled` | `true` | Kill-switch → pure cosine `match_findings`. |
| `full_text_weight` | `1.0` | RRF BM25-channel weight. |
| `semantic_weight` | `1.0` | RRF vector-channel weight. |
| `rrf_k` | `50` | RRF smoothing constant (Supabase canonical). |
| `reranker` | `"none"` | `"none" \| "llm" \| "ppr"` — **the single seam knob**. E7 registers `"ppr"`; it adds no `search.reranker`. |
| `rerank_model` | `"google/gemini-2.5-flash"` | Proven cheap gateway dot-slug (`config.yaml:98`). |
| `rerank_fallback_model` | `"openai/gpt-5.4-mini"` | Mirrors `config.py:238`. |
| `rerank_top_n` | `20` | Fused rows fed to the reranker. |
| `rerank_timeout_s` | `4.0` | Hard `asyncio.wait_for` bound. |
| `context_prefix_enabled` | `true` | Rung-1 master switch. |
| `context_prefix_max_chars` | `400` | ~100 tokens, per Anthropic's recipe. |

**`reranker` ships `"none"` in *both* the code default and `config.yaml` — one story, no dark-launch trick.** This is the deliberate correction of a draft contradiction: the draft claimed rung 3 was "zero behavior change until flipped" while shipping `"llm"` in yaml, which cannot both be true. The cited `user_profile` precedent (`config.py:219` default `False` vs `config.yaml:56` `enabled: true`) cuts *against* dark-launching, not for it — it is a config whose "off by default" documentation is already false in the shipped file. Rung 3 flips per deployment once E1 has latency/quality numbers.

## Error handling

| Condition | Behavior |
|---|---|
| FTS syntax error / empty tsquery (stopword-only query) | Semantic channel only; `rrf_score` degenerates to the vec term. Caught in-store, logged once. |
| Cloud RPC absent (migration lag) | `SupabaseStore.hybrid_match_findings` catches *function-not-found*, falls back to `match_findings` **with `categories` dropped and filtered client-side** (the deployed RPC has no such param), synthesizes `rrf_score` from rank. The engine never breaks on migration lag. |
| Any other hybrid failure | `retrieve` falls back to `store.match_findings`; logged at WARNING. |
| Rerank failure / timeout / bad parse | Fused order returned unchanged, ≤`limit`. Best-effort, mirroring the narration idiom (`config.py:291-296`). |
| Reranker returns fabricated/foreign rows | Caller filters output to the input id set before returning — the contract is enforced, not trusted. |
| Row with NULL embedding matches FTS | `similarity` COALESCEs to `0.0` (never `None`) — `band_findings`' sort cannot raise `TypeError`. Live cloud currently has 0 such rows, but backfill skips and partial-batch failures make it reachable. |
| Synopsis missing at persist time | Prefix omits `kb:` topics; never blocks the insert. |
| Backfill per-batch failure | Skipped, reported, resume offset printed; re-runs idempotent. |

## Testing & acceptance

1. **Fusion math**: in-memory `SQLiteStore`, hand-computed RRF for a known 2-channel fixture; asserted row order matches exactly, including a missing-rank (single-channel) row.
2. **FTS-only hits carry real `similarity`**; a NULL-embedding FTS hit returns `similarity == 0.0` (not `None`) and `band_findings` sorts the result set without raising.
3. **`min_similarity` parity**: for the same query/fixture, `hybrid_match_findings(min_similarity=x)` and `match_findings(min_similarity=x)` return identical *sets* when `full_text_weight=0` — fusion is the only difference.
4. **Shape parity across tiers**: row keys of `hybrid_match_findings` == `match_findings` keys ∪ `{rrf_score}`, asserted for `SQLiteStore` and for `SupabaseStore` against the fake client (`tests/fake_supabase.py` style, cf. `test_supabase_findings.py`).
5. **Cloud predicate parity**: fake-client test asserts a `discarded` row returned by neither RPC; after E2, likewise for an `invalidated_at IS NOT NULL` row.
6. **SQLite FTS maintenance**: `insert` → `update_finding` → the BM25 channel matches the **new** text and not the old (the E2-resolution-UPDATE staleness regression); `delete_finding` removes the fts row.
7. **Legacy JSON backfill**: a finding whose `content` is a JSON dict is BM25-searchable by a term in its *rendered* body and **not** by `"json"`/brace tokens.
8. **`contextual_text`**: prefix truncation at `context_prefix_max_chars`; disabled → byte-identical to `f"{title}\n{body}"`; missing synopsis/provenance degrade without raising.
9. **Rerank seam**: registry round-trip (`register_reranker`/`get_reranker`); `get_reranker("none")` and `get_reranker("<unknown>")` return `None`; the `"llm"` reranker's parse failure, timeout, and fabricated-row output each leave the fused order intact and length ≤ `limit` (mock gateway, no network).
10. **Preamble is rerank-free**: `select_preamble` produces byte-identical output with `reranker: "llm"` and `reranker: "none"` — proven with a reranker that would reorder if called.
11. **E1 measurement — the acceptance that matters.** Per-rung top-20 **retrieval-failure rate** on E1's golden sets, measured cumulatively: baseline → rung 1 → rung 1+2 → rung 1+2+3. Target: the direction and rough magnitude of Anthropic's **−35% / −49% / −67%** trend. Each rung reports its own delta; a rung that fails to improve is a finding, not a failure to hide. Golden **coverage-verdict stability** is asserted alongside (a rung must not flip verdicts on its own).
12. **Post-backfill recalibration**: E1's `calibrate_bands.py` re-runs after `--apply` and emits proposed `tiers:` values for the contextual embedding space; the reproduced off-topic-`rich` golden case still returns `sparse`/`gap`.
13. Existing suite green; `test_config.py` extended for the new section + `DLP_RETRIEVAL__*` override.

## Sequencing & dependencies

1. **Rung 2** (`hybrid_match_findings`, both tiers, migration, `retrieve`, C5 switches). Independent: no distribution shift, banding untouched, `hybrid_enabled` is the kill-switch.
2. **Rung 3** (`rerank.py` seam + `"llm"`). Inert on arrival — code default *and* yaml both `"none"`. Flip per deployment once E1 numbers exist.
3. **Rung 1** (C1 + C2 forward path) paired with an E1 recalibration window.
4. **C6 backfill** `--apply`, gated on E6's `list_finding_digests` + E2's `update_finding` None→keep fix.

**Graceful degradation:**
- **Without E1**: every rung still works; improvements are unmeasured and rung-1 bands may be miscalibrated. Mitigate by delaying `--apply` — the forward-path prefix alone shifts only newly written rows.
- **Without E2**: C2 lands twice (§C2) instead of once; the migration ships **without** the `invalidated_at` predicate (the column does not exist) and E2's migration adds it later per the §Store merge order; C6 must pass `content` back verbatim rather than relying on None→keep. Nothing else changes — E3 does not otherwise depend on E2.
- **Without E6**: rungs 1–3 ship; only the legacy-row backfill waits.
- **Without E7**: `"ppr"` is simply an unregistered name; `get_reranker` returns `None` and `retrieve` skips the stage.
- **E2's cloud pure-ADD guard** is orthogonal: it gates the *write* path, E3 changes the *read* path. `hybrid_match_findings` is correct on cloud the moment its migration is applied, guard or no guard.

## Open risks

1. **Mixed embedding spaces during the rung-1 rollout.** Contextual forward-path vectors interleave with raw legacy vectors until C6's `--apply` — and C6 is gated on E6, so the window may be long. *Resolution:* `context_prefix_enabled` is the switch. Keep it **false** until `list_finding_digests` exists, then flip it and run `--apply` in the same window; the script prints pre/post similarity drift so the window is observable. Rungs 2–3 are unaffected either way, so nothing else waits on this.
2. **BM25 quality on rendered bodies.** Findings are markdown with `**Label**:` scaffolding (`render.py:36-46`); those labels are indexed as tokens and add uniform noise across the corpus. *Resolution:* accepted — BM25 saturation makes corpus-wide constant terms near-worthless rather than harmful, and the E1 rung-2 measurement (#11) would expose it if wrong. The real pollution risk — raw JSON braces from legacy `content` — is fixed by the Python backfill (§SQLite) and covered by test #7.
3. **LLM rerank latency and cost on `delapan_search`.** *Resolution:* the always-on preamble path is out of reach by decision #3, so the blast radius is one interactive tool call. Bounded by `rerank_timeout_s=4.0` with fused-order fallback, capped at `rerank_top_n=20` rows, and shipped `"none"` so no deployment pays until E1 justifies it.

## Out of scope

Resolver hybrid candidates (E2 scopes it embedding-only by design; revisit after E1 measures rung 2), `match_kg_nodes` fusion, `kb_id=None` org-wide search on cloud (pre-existing RPC gap), a `categories`/null-kb fix for the deployed `match_findings` (noted as drift; folded in only if that RPC is touched for another reason), cross-encoder or hosted rerank providers, and E7's `"ppr"` implementation itself — this spec ships only the seam it plugs into.
