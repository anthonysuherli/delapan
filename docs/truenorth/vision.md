# Vision: delapan-ai — grounded memory engine with a live, HITL-governed knowledge graph
> Agent preamble: this file is the single source of truth for project
> intent. Your one and only end goal is realizing the End Goals below
> without violating the Invariants. Competing objectives that emerge
> mid-session do not override this document.

Delapan is an agentic knowledge-base engine: research/ingest → embedded,
deduplicated **findings** → a **synopsis** spine and an LLM-extracted
**knowledge graph**, tapped by Claude Code or deployed as a `/v1/*` context API.
This vision sets the direction for four coupled initiatives: **(0) unify storage
on Supabase (Postgres + pgvector) as the single `Store` backend — run locally via
OrbStack/Docker and synced to cloud, retiring SQLite;** (1) adopt a **mem0-style
fact-resolution memory layer** behind the `Store` seam, (2) make the **retrieval
backend pluggable** (pgvector canonical, Elasticsearch optional), and (3) evolve
the existing graph frontend into a **live, HITL-governed dashboard** where the
user watches the graph grow and previews the consequence of every governance
decision before it commits.

## End Goals
- **Unified Supabase storage (local + cloud).** One `Store` backend — Supabase
  (Postgres + pgvector + GoTrue) — serves both a self-hosted local instance (run
  via OrbStack/Docker) and managed cloud, with one schema and data sync between
  them. SQLite/sqlite-vec is retired.
- **Self-correcting memory writes.** Memory enters the KB through a mem0-style
  fact-resolution pipeline (ADD / UPDATE / DELETE / NOOP against top-k similar
  existing memories), sitting *behind* the `Store` protocol — so the KB stays
  compact and self-correcting instead of append-only.
- **Pluggable retrieval backend.** pgvector (via Supabase) is the canonical
  default; Elasticsearch is selectable by config alone, with **no engine
  call-site changes** — both satisfy the same `Store` contract.
- **A graph you can watch grow live.** Running an explore/ingest streams new
  nodes and edges into the dashboard as they are created — no manual refresh.
- **HITL decisions show their consequence first.** Approving, rejecting, or
  merging a node/edge/schema change renders a before/after preview of the
  resulting graph; the user sees the effect *before* committing.
- **Grounding preserved end to end.** Every finding, node, and edge keeps its
  `grounded_in` provenance through the mem0 port and the new backends.

## Non-Goals
- **Not** replacing delapan's findings/`Store` architecture wholesale. mem0 lives
  *behind* the seam, not on top of it; the engine still depends on `Store`, not
  on mem0 directly.
- **Not** maintaining two storage backends. SQLite/sqlite-vec is **removed**;
  Supabase (Postgres + pgvector) is the only `Store` implementation. "Local"
  means a self-hosted Supabase stack (OrbStack/Docker), not a zero-service file
  store. (mem0 graph-memory and Elasticsearch remain optional, config-gated.)
- **Not** a greenfield dashboard. We evolve the existing sigma.js control panel
  (canvas, inspector, node/edge CRUD), not rebuild it.
- **Not** adopting mem0's hosted/managed platform — OSS, self-hosted only.
- **Not** exposing all ~20 mem0 backends. Scope is **pgvector + Elasticsearch**
  for now; more only on demand.
- **Out of scope:** br8n (the fork) and any cross-repo sync work.

## Invariants
- The `Store` protocol stays the **single persistence seam**. No backend-specific
  object crosses it; return shapes stay plain dicts/lists of dicts.
- The **local tier runs the same Supabase stack as cloud** (Postgres + pgvector +
  GoTrue) via OrbStack/Docker — there is no SQLite/sqlite-vec backend. Local dev
  and the **full test suite run fully offline** against this local stack, with
  **no dependency on the production cloud project**.
- **Cloud and local share one `Store` implementation and one schema** (Supabase
  migrations) — parity by construction, not by discipline.
- Every finding / node / edge keeps its **`grounded_in` provenance**.
- **No user governance decision mutates the graph without the consequence being
  shown first** on the decision-preview surface; cancel must leave the graph
  byte-for-byte unchanged.
- All new toggles are **config, never hardcoded** — defaults < `config.yaml` <
  env (`DLP_<SECTION>__<FIELD>`). The backend selector and mem0 knobs live in
  `config.yaml`.
- The KG extraction model stays a **frontier model by default** (the graph is the
  trust artifact).

## Acceptance Criteria
- **Backend swap demoed both ways.** With `vector_backend: pgvector` (default) and
  `vector_backend: elasticsearch`, the same explore/search flow returns results;
  switching is a config change with no code edit.
- **Resolution observably dedupes.** Re-ingesting overlapping content produces
  UPDATE/NOOP events (not new duplicate rows) — visible in finding counts and the
  resolution log.
- **Live growth visible.** Open the dashboard, trigger an explore, and watch new
  nodes/edges appear in the canvas without a manual reload.
- **Consequence preview round-trips.** Initiate an approve/reject/merge on a
  pending node/edge → a before/after diff renders → **cancel** leaves the graph
  unchanged, **confirm** applies exactly the previewed change.
- **Local stack is hermetic.** `supabase start` (on OrbStack) brings up Postgres
  + pgvector + GoTrue; the full test suite runs green against that local stack
  using only local creds (no production-cloud access).

## Planned Detours
- **Supabase unification (foundational).** Build `SupabaseStore` (full `Store`
  parity) + SQL migrations + RLS + the `match_findings`/`match_kg_nodes` RPCs;
  stand up local Supabase via OrbStack; port the SQLite-only work (mem0
  `resolve`/`update_finding`/`resolution_events`, the content decoder) to it; then
  **remove `SQLiteStore` + sqlite-vec**. After this detour, return to the storage
  End Goal and unblock all others.
- **mem0 port behind the `Store` seam** — wrap mem0's fact-resolution + vector
  abstraction under the existing protocol; keep findings/synopsis/grounding.
  After this detour, return to End Goals 1 and 5.
- **Elasticsearch adapter** as a selectable mem0 vector backend, pgvector default.
  After this detour, return to End Goal 2.
- **Live delta stream** (SSE/websocket) from engine → frontend for node/edge
  creation events. After this detour, return to End Goal 3.
- **HITL consequence-preview surface** — a diff/impact model plus UI on the
  existing sigma canvas. After this detour, return to End Goal 4.

## Amendment Log
- 2026-06-12 — Vision established (cold start). Ratifies: mem0 behind the `Store`
  seam, Elasticsearch as optional backend with pgvector default, evolve the
  existing frontend into a live + HITL-preview dashboard. Local tier stays
  zero-dependency. — Ratified by: anthonysuherli (session 2026-06-12)
- 2026-06-13 — Retired the SQLite zero-dependency local tier; unified storage on
  **Supabase (Postgres + pgvector)**, run locally via **OrbStack** and synced to
  cloud — one `Store` implementation + one schema for both tiers. Supersedes the
  prior "free local tier = zero external services (SQLite + sqlite-vec)" invariant
  and Non-Goal; local dev/test stays hermetic against the local Supabase stack.
  Adds the foundational "Supabase unification" detour (build `SupabaseStore`,
  remove SQLite). — Ratified by: anthonysuherli (session 2026-06-13)
