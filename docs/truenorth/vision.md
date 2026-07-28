# Vision: delapan-ai — grounded memory engine with a live, HITL-governed knowledge graph
> Agent preamble: this file is the single source of truth for project
> intent. Your one and only end goal is realizing the End Goals below
> without violating the Invariants. Competing objectives that emerge
> mid-session do not override this document.

Delapan is an agentic knowledge-base engine: research/ingest → embedded,
deduplicated **findings** → a **synopsis** spine and an LLM-extracted
**knowledge graph**, tapped by Claude Code or deployed as a `/v1/*` context API.
This vision sets the direction for four coupled initiatives: **(0) keep two
storage tiers at strict `Store`-protocol parity — SQLite + sqlite-vec local /
open-core, Supabase (Postgres + pgvector) cloud;** (1) adopt a **mem0-style
fact-resolution memory layer** behind the `Store` seam, (2) make the **retrieval
backend pluggable** (pgvector canonical, Elasticsearch optional), and (3) evolve
the existing graph frontend into a **live, HITL-governed dashboard** where the
user watches the graph grow and previews the consequence of every governance
decision before it commits. A fifth concern runs across all of them: **the
knowledge base must stay navigable as it grows** — reversible archiving and
activity reporting you can trust.

## End Goals
- **Two tiers at protocol parity.** SQLite + sqlite-vec is the local/open-core
  tier; Supabase (Postgres + pgvector) is the cloud tier. Both implement the full
  `Store` protocol with identical return shapes — the engine cannot tell them
  apart. Neither tier is retired.
- **Reversible KB lifecycle.** Projects and KBs can be archived and unarchived
  through the `Store` seam — never hard-deleted — so the workspace stays
  navigable as it grows. Discovery surfaces report activity derived from actual
  finding writes, so "what is dormant" is answerable from data rather than
  guessed.
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
- **Hosted public tier with account isolation.** delapan.ai serves a public
  landing page at `/` and an account-gated dashboard at `/app`: self-serve
  sign-up (email+password and GitHub OAuth via Supabase Auth), one org per user
  on the existing `org_members` + RLS rails, and an authenticated engine API
  giving the dashboard real (non-mock) data. The cloud tier launches as a free,
  invite-gated beta.

## Non-Goals
- **Not** replacing delapan's findings/`Store` architecture wholesale. mem0 lives
  *behind* the seam, not on top of it; the engine still depends on `Store`, not
  on mem0 directly.
- **Not** letting the two storage backends diverge. Both are maintained, but
  neither may drift: a `Store` method that lands on one tier and not the other is
  an unfinished feature, not a tier-specific one. (mem0 graph-memory and
  Elasticsearch remain optional, config-gated.)
- **Not** hard deletion, purge, or GC of projects/KBs. Archiving sets state; it
  never removes rows.
- **Not** a greenfield dashboard. We evolve the existing sigma.js control panel
  (canvas, inspector, node/edge CRUD), not rebuild it.
- **Not** adopting mem0's hosted/managed platform — OSS, self-hosted only.
- **Not** exposing all ~20 mem0 backends. Scope is **pgvector + Elasticsearch**
  for now; more only on demand.
- **Not** billing at launch. No Stripe until beta usage justifies it; the
  open-core local tier is the public free story.
- **Not** multi-member organizations at launch. Org-per-user only; invites,
  roles, and team management are post-launch.
- **Not** retiring the Canvas v1 draft (2026-07-17, unratified). It remains a
  candidate later release; the hosted-tier amendment neither ships nor blocks
  it.
- **Out of scope:** br8n (the fork) and any cross-repo sync work.

## Invariants
- The `Store` protocol stays the **single persistence seam**. No backend-specific
  object crosses it; return shapes stay plain dicts/lists of dicts.
- Every `Store` method lands on **both** backends with identical return shapes
  before the feature is done. Parity by construction, not by discipline.
- The **offline test suite runs hermetically against the local SQLite tier**,
  with **no dependency on the production cloud project**.
- Every finding / node / edge keeps its **`grounded_in` provenance**.
- Archiving is **always reversible and non-destructive**: it sets state, never
  removes findings, nodes, edges, or `grounded_in` provenance. No delapan surface
  hard-deletes a project or KB.
- Activity and coverage metrics derive from **first-class engine writes**, never
  from a category or convention owned by a fork or downstream client.
- **No user governance decision mutates the graph without the consequence being
  shown first** on the decision-preview surface; cancel must leave the graph
  byte-for-byte unchanged.
- All new toggles are **config, never hardcoded** — defaults < `config.yaml` <
  env (`DLP_<SECTION>__<FIELD>`). The backend selector and mem0 knobs live in
  `config.yaml`.
- The KG extraction model stays a **frontier model by default** (the graph is the
  trust artifact). Cost pressure is never sufficient reason to lower it. This
  invariant scopes to `knowledge_graph.extraction_model` alone — the exploration,
  deepen, synopsis, and narration lineups carry no frontier requirement and may be
  tuned for cost.
- **AI spend is bounded and observable by construction.** Every pipeline reaching
  the AI Gateway runs under an explicit pre-request budget ceiling, and every LLM
  call is metered regardless of entry path (HTTP API, MCP tool, or direct
  process). A pipeline that can spend without a ceiling, or spend without being
  recorded, is an unfinished feature.
- Every cloud table carrying tenant data has **org-scoped RLS on reads and
  writes (`WITH CHECK`), verified by audit/test** — isolation by construction,
  not convention.
- The **local open-core tier stays auth-less.** Auth is a cloud-tier concern;
  no local workflow ever requires an account.

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
- **Local suite is hermetic.** The full test suite runs green offline against the
  SQLite tier, with no production-cloud access.
- **Archive round-trips.** Archive a KB → it drops out of default
  `delapan_projects` output → unarchive → it returns with its finding count
  unchanged. Same behaviour on both backends.
- **Activity reporting is truthful.** `delapan_projects` reports non-zero recent
  activity for any KB written to today.
- **Stranger round-trip.** Landing → invite code → sign-up (email or GitHub) →
  verified email → first-run empty state → first populated graph, with no
  operator intervention.
- **Isolation holds.** A second account can neither read nor write the first
  account's projects, KBs, findings, or graph — verified by test, not
  inspection.
- **Local tier unaffected by auth.** The local dashboard workflow still works
  with zero auth configuration.
- **Hardening minimum live before the sign-up link is public:** ToS + privacy
  policy, custom SMTP for auth email, rate limiting on public endpoints, error
  tracking on backend + frontend, backups verified.

## Planned Detours
- **KB lifecycle + truthful activity.** Add reversible archive state for projects
  and KBs across both backends, and rebuild `list_projects` to report
  `finding_count` / `last_finding_at` from real finding writes instead of the
  br8n-owned `category='snapshot'` convention. After this detour, return to End
  Goal 2 (reversible KB lifecycle).
- **OrbStack local Supabase (optional).** Stand up a local Supabase stack so the
  cloud code path can be tested hermetically instead of via `fake_supabase.py` or
  opt-in production smoke. Does not change which tier is canonical. After this
  detour, return to End Goal 1 (protocol parity).
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
- 2026-07-16 — End Goal **"Self-correcting memory writes" is DELIVERED**:
  findings now resolve ADD/UPDATE/NOOP/SUPERSEDE against the KB at write time
  (`core/memory/`) — SUPERSEDE instead of the hard DELETE this vision specified,
  via bi-temporal `valid_from`/`invalidated_at`/`superseded_by` so nothing is ever
  removed, only retired. **Not** delivered as this vision assumed: it shipped on
  BOTH SQLite and Supabase, not Supabase-only — the SQLite retirement End Goal 0
  calls for has not happened, and this change instead gave SQLite full
  write-primitive parity with the cloud `Store`. The codebase has now moved away
  from End Goal 0 twice (also flagged in the delapan-82 fork's design spec,
  2026-07-08, which explicitly reverses this vision's Supabase-unification
  direction) — this vision's storage End Goal is stale and should be revisited,
  not silently treated as still in effect. — Ratified by: anthonysuherli
  (session 2026-07-16)
- 2026-07-18 — **Resolved the stale storage End Goal 0.** Replaced "unified
  Supabase storage / SQLite retired" with **two tiers at strict `Store`-protocol
  parity** (SQLite + sqlite-vec local/open-core, Supabase cloud; neither
  retired). This codifies the shipped system rather than an aspiration: SQLite is
  1,276 lines implementing 38/38 protocol methods with zero deprecation markers,
  the backend selector defaults to SQLite when cloud creds are absent, no local
  Supabase stack exists (no `config.toml`, no docker-compose — backlog only), and
  the offline suite runs on SQLite while Supabase is covered by `fake_supabase.py`
  or opt-in production smoke. Sequencing confirmed the intent: SQLite received the
  bi-temporal write primitives first (`f60b6b4`, `0429924`), then Supabase was
  brought to parity (`ba8eda5`). `docs/tracking/initiatives/supabase-storage-
  unification.md` had already marked the goal `blocked`. Retired the "Supabase
  unification (foundational)" detour; OrbStack local Supabase survives as an
  optional test-hermeticity detour only.
  **Added End Goal "Reversible KB lifecycle"** — archive/unarchive for projects
  and KBs through the `Store` seam, never hard delete, plus activity reporting
  derived from real finding writes. Motivated by 83 KBs across 29 projects with
  no delete or archive mechanism anywhere in the codebase, and by
  `list_projects` ranking every KB by `category='snapshot'` — a br8n-owned
  convention that nothing has written since 2026-06-09, while 3,321 other
  findings landed through 2026-07-18, making every KB look dormant.
  — Ratified by: anthonysuherli (session 2026-07-18)
- 2026-07-19 — **Added End Goal "Hosted public tier with account isolation"**:
  landing at `/`, account-gated dashboard at `/app`, sign-up via email+password
  and GitHub OAuth (Supabase Auth), org-per-user on the existing `org_members`
  + RLS rails, authenticated engine API (one `/api` contract for both tiers —
  cloud verifies Supabase JWTs, local stays auth-less), free invite-gated beta.
  Added invariants: org-scoped RLS verified on every tenant table; local tier
  never requires an account. Added non-goals: no billing at launch, no
  multi-member orgs at launch, Canvas v1 draft unaffected (remains a candidate
  later release). Design principle carried into the spec: **consider free,
  already-available managed services before building** (Supabase Auth flows,
  Sentry, uptime/status, analytics free tiers). Motivated by: deployed
  delapan.ai serving mock data with no auth; hosted rails (Supabase Auth /
  OAuth 2.1, `org_members`, org-scoped RLS) already live in the cloud MCP
  tier; 2026-07-19 launch-readiness research (24 findings → `delapan/master`,
  full report in the public-release spec). — Ratified by: anthonysuherli
  (session 2026-07-19)
- 2026-07-27 — **Scoped the frontier-extraction invariant and added a spend
  invariant.** The frontier requirement now explicitly covers
  `knowledge_graph.extraction_model` only (currently `anthropic/claude-opus-4.8`),
  making clear that the exploration, deepen, synopsis, and narration lineups carry
  no frontier obligation and may be tuned for cost. Motivated by a Vercel/AI-Gateway
  cost review: the invariant was being read as blocking all model-cost work, but KG
  builds are low-frequency full rebuilds with bounded cost, while the actual burn
  sits in paths the invariant never covered — `deepen.decompose_model` and
  `deepen.critic_model` (both Opus 4.8, up to 3 rounds × 4 facets; the largest
  metered line item observed) and `exploration.reasoning_effort: high`. Note
  `knowledge_graph.reasoning_effort` is `null` and independent of the exploration
  knob, so exploration effort changes never touch KG extraction.
  **Added invariant "AI spend is bounded and observable by construction"** — every
  Gateway-reaching pipeline runs under a pre-request budget ceiling and every LLM
  call is metered regardless of entry path. Motivated by two findings this session:
  a $0 Gateway credit balance returns HTTP 402 on *every* engine call (taking down
  explore, synopsis, and chat alike, visible only in process logs) with no budget
  guard anywhere; and `usage_events` recorded zero rows for three explores run via
  the fresh-process/MCP path, so `/ops/costs` under-reports real burn.
  — Ratified by: anthonysuherli (session 2026-07-27)
