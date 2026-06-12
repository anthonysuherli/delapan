# Design: mem0-style memory resolution behind the `Store` seam

**Status:** Approved (brainstorming) — ready for implementation planning
**Date:** 2026-06-12
**Vision:** [docs/truenorth/vision.md](../vision.md) — Detour 1 of the ratified vision
**Vision goals served:** End Goal 1 (self-correcting memory writes) and End Goal 5 (grounding preserved end to end).

## Problem

Delapan's finding write path is **append-only against the KB**. The exploration
pipeline produces `Finding` objects; [`FindingMerger`](../../../delapan/core/exploration/merger.py)
deduplicates only *within a single explore batch* (cluster by category + fuzzy
title), and both persist call sites then do a pure INSERT via
[`store.insert_findings`](../../../delapan/store/base.py). Nothing resolves a new
finding against findings **already stored** from prior runs, so re-exploring an
overlapping topic accumulates near-duplicate findings over time. The `Store`
protocol has **no `update_finding`** — findings are immutable once written
(delete-only).

mem0's contribution is exactly the missing step: a **resolution pass between
within-batch merge and persistence** that, for each candidate, retrieves the
top-k semantically similar existing findings and lets an LLM decide whether to
ADD, UPDATE, NOOP, or DELETE — keeping the KB compact and self-correcting.

## Scope

Locked during brainstorming:

- **Port the pattern, no mem0 dependency.** Reimplement the resolver natively
  using delapan's existing AI-gateway/Anthropic client + the `Store` contract.
  mem0 is the design reference only.
- **Findings only.** The KG keeps its existing dedup + drift machinery and is
  grown/rebuilt from the now-resolved findings (graph-level governance is detour 4).
- **In-place + resolution log.** UPDATE overwrites the existing finding with a
  STABLE id (KG grounding stays valid); every decision is appended to a
  `resolution_events` log for observability. No finding versioning.
- **Both tiers.** Resolution runs in the engine above the `Store`, so it works
  identically on the local SQLite tier and the Supabase cloud tier.
- **Elasticsearch is out of scope here** (detour 2). The resolver is
  backend-agnostic and lands first; Elastic slots under the `Store` seam later
  with no resolver change.

## Architecture

New package `delapan/core/memory/`, sitting between `FindingMerger` and persistence:

```
delapan/core/memory/
├─ __init__.py
├─ models.py     ResolutionOp, ResolutionDecision, ResolutionEvent, ResolutionOutcome
├─ resolver.py   the brain: retrieve neighbors + one LLM pass → decisions
└─ persist.py    resolve_and_persist(): render+embed → resolve → apply → log → return ids
```

Each unit has one job and depends only on well-defined interfaces:

- **`resolver.py`** depends on the `Store` contract (`match_findings`) + the
  embeddings/LLM clients. Pure decision logic — given candidates and their
  neighbors, returns `ResolutionDecision`s. No persistence side effects.
- **`persist.py`** orchestrates: renders + embeds candidates, calls the resolver,
  applies decisions through the `Store`, writes the log, and returns the affected
  finding ids. This is the single function both call sites invoke.

### Data flow

```
run_exploration → [Finding…] → FindingMerger (within-batch, UNCHANGED)
      │
      ▼  resolve_and_persist(ctx, store, candidates, cfg)
   render + embed candidates (embed_batch)
      ▼
   store.match_findings(kb_id, emb, neighbor_top_k, neighbor_min_similarity)
        ─► top-k existing neighbors per candidate
      ▼
   ONE LLM pass → per candidate: ADD | UPDATE(target_id) | NOOP | DELETE(target_id)
      ▼  apply:
   ADD    → store.insert_findings([row])              (new id)
   UPDATE → store.update_finding(target_id, merged)   (id STABLE → KG grounding intact)
   NOOP   → skip
   DELETE → store.delete_finding(target_id)           (rare; direct contradiction only)
      ▼
   store.insert_resolution_events(kb_id, events)      ─► resolution_events (audit)
      ▼ returns ResolutionOutcome.affected_finding_ids = added ∪ updated
   update_exploration(finding_ids=affected) → maybe_rebuild_synopsis → schedule_kg_update(affected)
```

## Components

### `models.py`

- `ResolutionOp` — `Enum`: `ADD`, `UPDATE`, `NOOP`, `DELETE`.
- `ResolutionDecision` — `{op: ResolutionOp, target_finding_id: str | None, reason: str}`.
  `target_finding_id` is required for UPDATE/DELETE, `None` for ADD/NOOP.
- `ResolutionEvent` — a log row: `{op, candidate_title, target_finding_id, reason}`.
- `ResolutionOutcome` — `{affected_finding_ids: list[str], events: list[ResolutionEvent]}`.

### `resolver.py`

```python
async def resolve(
    store: Store,
    kb_id: str,
    candidates: list[Finding],
    embeddings: list[list[float]],
    cfg: MemoryConfig,
) -> list[ResolutionDecision]:
    ...
```

- For each candidate, retrieve neighbors via `store.match_findings(kb_id, emb,
  cfg.neighbor_top_k, cfg.neighbor_min_similarity)`.
- Build ONE LLM prompt covering the batch: each candidate (title + rendered
  content) paired with its neighbors (`id, title, content snippet, similarity`).
- The LLM returns one decision per candidate: `{op, target_finding_id?, reason}`.
  Prompt adapted from mem0's update-memory prompt (ADD/UPDATE/NONE/DELETE
  semantics), made structured.
- Candidates whose neighbor set is empty short-circuit to ADD without consuming
  an LLM slot.

### `persist.py`

```python
async def resolve_and_persist(
    ctx: TenantContext,
    store: Store,
    candidates: list[Finding],
    cfg: AppConfig,
) -> ResolutionOutcome:
    ...
```

- Renders each candidate's content (reusing the existing `_render_content`
  helper — to be lifted to a shared location so both call sites and this module
  use one copy), embeds in one `embed_batch`.
- Calls `resolve(...)`, applies decisions, writes `resolution_events`, returns
  the outcome.
- **Replaces** the duplicated persist block in both call sites
  ([routes_explore.py:130-158](../../../delapan/api/routes_explore.py),
  [mcp/server.py:156-190](../../../delapan/mcp/server.py)). Those sites keep
  ownership of `update_exploration` / `maybe_rebuild_synopsis` /
  `schedule_kg_update`, now fed by `outcome.affected_finding_ids`.

## New `Store` surface

Added to the `Store` Protocol and BOTH implementations (`SQLiteStore`,
`SupabaseStore`), preserving return-shape parity.

### `update_finding`

```python
async def update_finding(
    self, kb_id: str, finding_id: str, *,
    content, confidence, provenance, embedding, title: str | None = None,
) -> None
```

- In-place overwrite of the row; **id stays stable** so KG `grounded_in`
  references remain valid. Re-indexes the vector (SQLite: delete+insert the
  `vec_findings` row; Supabase: update the embedding column).
- The store method is a straight overwrite; `persist.py` computes the new values
  before calling it. **v1 UPDATE semantics** (fixed to remove ambiguity): it reads
  the target via `store.get_finding`, then sets `content := candidate content`
  (newer information wins), `provenance := union(old, new)`, `confidence :=
  recomputed from the unioned source count` (via `merger.confidence_from_sources`),
  and `embedding := the candidate's already-computed embedding` (no re-embed). The
  LLM decides *which* existing finding to update and *why*; it does **not** author
  consolidated prose in v1 (deferred). `title` updates when the candidate's differs.

### Resolution log

New table `resolution_events`:

| column | type |
|---|---|
| id | text PK |
| org_id | text |
| kb_id | text |
| op | text (ADD/UPDATE/NOOP/DELETE) |
| candidate_title | text |
| target_finding_id | text null |
| reason | text |
| created_at | text/timestamptz |

Methods:
- `async def insert_resolution_events(self, kb_id: str, events: list[dict]) -> None`
- `def list_resolution_events(self, kb_id: str, limit: int | None = None) -> list[dict]`

SQLite: add the table to `_SCHEMA` (+ a `kb_id` index). Supabase: a new
migration mirroring the columns. `delete_finding` already exists and serves
DELETE.

## Configuration

New `memory:` section in `config.yaml`, surfaced as `MemoryConfig` on `AppConfig`,
overridable via `DLP_MEMORY__<FIELD>`:

```yaml
memory:
  enabled: true                         # kill-switch: false → pure ADD (today's behavior)
  resolution_model: anthropic/claude-sonnet-4.6   # capable judgment, cheaper than the Opus the KG uses; resolution runs every explore
  resolution_fallback_model: google/gemini-3.1-pro-preview
  neighbor_top_k: 5                     # existing findings retrieved per candidate
  neighbor_min_similarity: 0.6          # floor for a neighbor to be considered
  max_candidates_per_pass: 25           # batch cap for one LLM resolution pass
  temperature: 0.0
```

No knob is hardcoded (vision invariant). Defaults < `config.yaml` < env.

## Error handling

A safe, reversible rollout — resolution can never lose knowledge:

- **Resolver LLM fails or returns malformed output → ADD-all fallback** (today's
  append behavior). Logged. A resolution failure never drops a finding.
- **`memory.enabled = false` → pure ADD**, byte-for-byte today's behavior. This is
  the rollout/regression kill-switch.
- **DELETE is conservative:** only the matched `target_finding_id`, only on direct
  contradiction. KG nodes grounded in a deleted finding tolerate the missing id
  (the builder's `_gather_findings` already skips absent ids).
- Embedding failures preserve current behavior (the explore fails as it does
  today); the resolver fallback covers only the resolution LLM step.

## Testing

| Level | Test | Asserts |
|---|---|---|
| Resolver unit | stub LLM returns each op | correct `Store` calls per op; UPDATE/DELETE carry a target |
| Resolver unit | malformed LLM output | ADD-all fallback; no finding dropped |
| Resolver unit | empty neighbor set | short-circuits to ADD, no LLM call |
| Store unit | `update_finding` round-trip (SQLite) | fields + vec row replaced, id stable |
| Store unit | `resolution_events` insert/list (SQLite) | rows round-trip, scoped to kb_id |
| Integration | local SQLite, no services: ingest then re-ingest overlapping content | UPDATE/NOOP events, **no duplicate row**, counts reflect resolution |
| Parity | Supabase `update_finding` + `resolution_events` | same shapes as SQLite |
| Regression | `enabled=false` | reproduces append behavior; existing explore tests pass |

The integration test is **vision Acceptance Criterion #2** ("re-ingesting
overlapping content produces UPDATE/NOOP events, not duplicates — visible in
finding counts and the resolution log"), runnable on the zero-dependency local
tier (Acceptance: "local tier suite passes with no Docker/Neo4j/ES").

## In-scope cleanup

The two duplicated persist blocks (`routes_explore.py`, `mcp/server.py`) collapse
into `resolve_and_persist`, and `_render_content` / `_normalize_provenance` move
to a shared location instead of being duplicated. This is a DRY win the resolver
directly enables — not unrelated refactoring.

## Out of scope

- Elasticsearch backend (detour 2).
- KG node/edge resolution and graph-level HITL (detour 4).
- Finding versioning / full history (the resolution log covers the audit need).
- Decay / time-based forgetting (a possible later mem0 feature; not requested).
