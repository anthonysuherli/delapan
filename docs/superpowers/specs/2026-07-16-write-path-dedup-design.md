# Write-path integrity: persist-time resolution with bi-temporal supersede + eval harness

*2026-07-16 · Status: approved design, pre-implementation · Line: master · Portfolio items E1+E2 from `delapan-ai/reports/2026-07-15-delapan-enhancement-brainstorm.md` · Adversarially verified against the codebase in two rounds (4+2 lenses, 45 findings folded in).*

## Problem

Findings are append-only. `SQLiteStore.insert_findings` is a plain INSERT; the only dedup is `FindingMerger`'s fuzzy-title match (`SequenceMatcher ≥ 0.80`, per-category) **within a single exploration run**. Re-running an explore on the same KB inserts near-duplicate rows; contradicted findings are never retired; single-source findings sit at `confidence ≈ 0.2–0.3` forever because corroboration is discarded instead of merged. And none of the core heuristics (`band_findings`, `assess_coverage`, `select_preamble`, `FindingMerger`) have tests, so no fix is provable. Coverage bands (`.55/.40/.25`) were tuned for `text-embedding-3-small` but the engine now embeds with `google/gemini-embedding-001` — observed failure: a resume query returned off-topic findings under a `rich` verdict.

External grounding (adversarially verified, see brainstorm report §2): production memory engines (Mem0, Zep/Graphiti) converged on exactly this loop — extract → resolve against existing → corroborate or supersede. Graphiti's verified pattern for contradictions is **temporal invalidation, not deletion**.

## Decisions (approved 2026-07-16)

1. **Scope**: E1 (eval harness) + E2 (persist-time resolution) only. Hybrid FTS retrieval (E3), gap flywheel (E4), deepen (E5), KG changes (E7) are out of scope.
2. **Basis**: adopt `feat/mem0-resolution-port` (resolver, persist path, config, events log, tests) and extend it; do not redesign.
3. **Line**: delapan master. delapan-82 hand-ports later per fork rules.
4. **Eval ambition**: unit tests + golden query sets + band-calibration script. No LLM-judge scorecard.
5. **Backfill**: forward-path resolution for all new inserts, plus a standalone per-KB backfill script (dry-run by default). No auto-migration.

**Known constraint surfaced by verification:** `feat/mem0-resolution-port` forked **before** `feat/supabase-store` merged, so it implements the new Store methods only on `SQLiteStore` + the Protocol. `SupabaseStore` has none of `update_finding` / `insert_resolution_events` / `list_resolution_events`. Cloud-tier client work is therefore an explicit component (C6), and resolution stays disabled on cloud until it lands. Because `memory.enabled` is a single global knob while `active_backend()` picks the tier per-process (creds sniff), the cloud gate is a **code guard**: `resolve_and_persist` forces the pure-ADD path whenever `active_backend() == "cloud"`, removed only at Rollout step 5 once C6 + the migration are verified.

## Architecture

```
explore candidates ─► render + embed ─► resolve() one LLM pass ─► apply ops ─► events log
                                            │                        │
                              neighbors: match_findings          ADD       → insert row
                              top-k=5, sim ≥ 0.6                 NOOP      → corroborate target (provenance union,
                                                                             monotonic confidence raise; no-write if no new URL)
                                                                 UPDATE    → supersede_finding(target, new row w/ merged provenance)
                                                                 SUPERSEDE → supersede_finding(target, new row w/ own provenance)
retrieval (resume/search/preamble): match_findings WHERE invalidated_at IS NULL
```

`resolve_and_persist` (`delapan/core/memory/persist.py`, from the branch) stays the **single** persist path for both call sites (MCP `delapan_explore`, API `_run_and_persist`) — the branch's DRY refactor lands with the merge. `memory.enabled: false` remains a pure-ADD kill-switch (rows identical to today's apart from the three new columns of §C3). **Cloud guard**: until C6 + the migration are verified, `resolve_and_persist` also forces pure ADD whenever `active_backend() == "cloud"`, independent of `memory.enabled` (removed at Rollout step 5).

## Components

### C1 · Prerequisite: resolver sees neighbor bodies

Master **already** round-trips string content (commit `398c29a`: `_json_load(r["content"], r["content"])` in both `_finding_from_row` and `match_findings`); the separate `fix/finding-content-roundtrip` branch is redundant and conflicts — **do not merge it**. Cherry-pick only its regression test, resolving in master's favor. The real C1 work: update `resolver.py`'s stale module docstring ("neighbor bodies are unreliable") and extend `_candidate_block` to include neighbor bodies under the same 600-char budget as candidate bodies, with the system prompt adjusted to use them.

### C2 · Resolver op semantics (delta vs branch)

`ResolutionOp` becomes `ADD | UPDATE | NOOP | SUPERSEDE` (`DELETE` removed; the LLM system prompt in `resolver.py` reworded to match). Application in `persist.py`:

| Op | Branch behavior | New behavior |
|---|---|---|
| ADD | insert row | unchanged |
| NOOP | log-only, candidate discarded | **corroborate**: union candidate provenance into target (`_merge_provenance`). If the union gains ≥1 new distinct URL: `confidence = max(target.confidence, confidence_from_sources(distinct urls in merged provenance))` — monotonic, never lowers, and a same-URL duplicate is a true no-write (idempotent re-runs leave rows untouched). URL-less provenance entries are preserved but do not raise the source count. Candidate body discarded. |
| UPDATE | in-place overwrite via `update_finding` (id stable) | **version**: one atomic `supersede_finding` call — insert candidate as a new row, set target's `invalidated_at = now`, `superseded_by = <new id>`. New row: merged provenance (target ∪ candidate), `confidence = confidence_from_sources(distinct urls of the union)` (explicitly replaces the old value — content changed, the old blend no longer applies). |
| DELETE | `delete_finding` (destructive) | **SUPERSEDE**: same `supersede_finding` mechanics. New row: the candidate's **own** normalized provenance only and `confidence_from_sources` over its own distinct URLs — a contradicted finding's sources must not corroborate the claim that contradicts them. |

Notes:
- **Write primitives** (this spec's vocabulary, used by C5/C6): `supersede_finding(kb_id, target_id, new_row) -> new_id` — insert new row then invalidate target, **in one transaction** (SQLite) / one RPC (cloud, §C6); `invalidate_finding(kb_id, finding_id, superseded_by)` — retire a row in place with no insert (needed by backfill; a plain column update under RLS on cloud); and `update_finding` with **None→keep semantics defined here for both tiers**: `content` / `embedding` / `title` = `None` means keep the current value (the branch's `SQLiteStore.update_finding` overwrites `content` unconditionally and must be amended — `content=None` currently NULLs the body). All three land on the Protocol, `SQLiteStore`, and `SupabaseStore` (§C6).
- **NOOP corroborate is** `update_finding(provenance=merged, confidence=raised, content=None, embedding=None, title=None)`.
- **Atomicity**: `supersede_finding` inserts first, then invalidates, so a rollback can only leave the KB unchanged. Its inserts are eager, outside `persist.py`'s deferred `add_rows` batch (the new id is needed for the pointer). A failed call logs the op as **failed** with the target id and error.
- `affected_finding_ids` carries the **new** row's id for UPDATE/SUPERSEDE (the target id appears only in the event log), ADD ids as today; NOOP contributes no affected id.
- **Stale targets**: resolver-level unknown-target demotion to ADD is branch behavior and kept. At the persist layer the branch degrades only UPDATE to ADD; this change **extends** that to SUPERSEDE (the branch's DELETE arm silently dropped the candidate — that violates "a resolution failure must never drop a finding" and is not kept). NOOP with a stale target skips the write and logs the miss.
- Resolver failure of any kind degrades to ADD-all (branch behavior, kept).
- **Events as audit**: `resolution_events` gains a nullable `new_finding_id` and a nullable `details` JSON field (NOOP records merged URLs and confidence before/after) so every op's effect is reconstructible; `op` records `SUPERSEDE` instead of `DELETE`. The log records decisions **actually applied** (failed ops log as failed). A **gated no-write NOOP still logs its event** (`details` records zero new URLs, confidence unchanged) — "no-write" applies to the findings table only.
- KG safety: because SUPERSEDE keeps the old row, existing `grounded_in` references in `kg_nodes` stay resolvable — an improvement over the branch's DELETE, which broke them. `get_finding` returns invalidated rows (with `invalidated_at`/`superseded_by` visible) so KG hydration and provenance chains keep working.

### C3 · Schema (both tiers)

Findings gain three nullable columns:

| Column | Type | Meaning |
|---|---|---|
| `valid_from` | timestamp | when the fact became current |
| `invalidated_at` | timestamp, NULL = live | when it was superseded/contradicted |
| `superseded_by` | finding id, nullable | forward pointer to the replacing row |

- **`valid_from` is stamped by the write path** (`insert_findings` and `supersede_finding`, both tiers) for **all** inserted rows including kill-switch ADDs — SQLite's `ALTER TABLE ADD COLUMN` cannot carry a non-constant default, so the column default is Supabase-only belt-and-braces. Existing rows backfill from `created_at`.
- **SQLite**: three findings entries appended to `_ADD_COLUMN_MIGRATIONS` (`store/sqlite.py`), plus an idempotent `UPDATE findings SET valid_from = created_at WHERE valid_from IS NULL` run in the same migration pass. **`resolution_events` also changes shape here**: `new_finding_id TEXT` and `details TEXT` are added both to the `_SCHEMA` CREATE TABLE and as two more `_ADD_COLUMN_MIGRATIONS` entries (DBs created during the dark window have the branch's 8-column table), with matching `ResolutionEvent` model fields and updated insert/list column lists in `sqlite.py`. No local partial index — brute-force scan is fine at local scale.
- **Supabase**: migration SQL below (§ Migration). Tables/RPCs live outside this repo; the change is not complete until applied to the cloud project.
- `match_findings` adds `AND invalidated_at IS NULL` (SQLite query + the cloud RPC). No caller-facing history flag on `match_findings`; history is reachable via `get_finding` and `list_findings(include_invalidated=True)` (new optional param, default `False`).
- **`count_findings` counts live rows only** (`invalidated_at IS NULL`, both tiers) — it drives synopsis `rebuild_delta` and is the row-count metric for acceptance #3/#7.

### C4 · E1 eval harness

**(a) Unit tests** (all pure, no IO, no network):
- `band_findings` / `assess_coverage`: band boundaries, empty input, verdict transitions (`tests/test_preamble.py`).
- `select_preamble` budget accounting: char ceiling honored, lowest-similarity dropped first, XML escaping (pure parts factored as needed).
- `FindingMerger`: fuzzy-title clustering, provenance union, `confidence_from_sources` / `blended_confidence` math (`tests/test_merger.py`).
- Resolver application: each op's store effect, stale-target degradation paths, NOOP no-write idempotence, kill-switch path (`tests/test_memory_persist.py` extended from branch).

**(b) Golden query sets** — `tests/golden/<name>.yaml`: a findings fixture and queries with `expect_verdict` and `expect_top_ids`. **Both finding and query vectors are recorded** — `scripts/gen_golden_embeddings.py` embeds findings *and* queries with the live model once and writes vectors to a JSON sidecar (reduced float precision; verdicts are threshold-robust) referenced from the YAML, keeping fixtures reviewable. The pytest runner seeds a temp `SQLiteStore`, feeds the recorded query vector directly to `match_findings` → `band_findings` → `assess_coverage` (no embedding client), and runs fully offline in CI. First two sets: a `delapan-engine` set (seeded from real master-KB findings) reproducing the off-topic-but-rich failure, and a synthetic disjoint-topics set.

**(c) Calibration script** — `scripts/calibrate_bands.py`: calibrates on **query→finding** similarities (the quantity the bands actually gate): representative queries (golden-set queries, past exploration prompts, resume queries) against their own KB as the positive class, and the same queries against a **different** KB as the off-topic negative class. Prints both distributions and proposes `band1/2/3` thresholds at the separation point; pairwise finding↔finding similarity, if printed at all, is a sanity check only. Output is advisory; chosen values land in `config.yaml` under `tiers:` with a comment naming the embedding model they were calibrated for. Recalibrating against `delapan/master` is part of this update's acceptance.

### C5 · Backfill script

`scripts/dedup_backfill.py <project> <kb> [--apply] [--batch N]` replays a KB's live findings **oldest-first**. It reuses `resolve()`'s decision logic but with two backfill-specific mechanics (the forward applier cannot be reused verbatim — a backfill candidate already exists as a live row):

- **Neighbor restriction**: the script requests a larger `match_count`, then post-filters each candidate's neighbor hits to ids **earlier in the oldest-first replay order and still live** (batch-size-independent) — excluding the candidate's own row (which would match itself at sim ≈ 1.0) and all later rows.
- **Backfill op applier** (built from the §C2 write primitives): ADD → keep the original row, no insert. NOOP target=t → merge candidate's provenance into `t` per §C2, then `invalidate_finding(candidate, superseded_by=t)`. UPDATE target=t → the candidate (newer, already persisted) becomes the survivor: first union `t`'s provenance into the candidate via `update_finding` with `confidence = confidence_from_sources(distinct URLs of the union)` — mirroring forward-UPDATE so no URL leaves the live set — then `invalidate_finding(t, superseded_by=<candidate id>)`. SUPERSEDE target=t → `invalidate_finding(t, superseded_by=<candidate id>)` with **no merge**, mirroring §C2's contradiction rule. Backfill ops log to `resolution_events` with the same op vocabulary.
- **Embeddings**: candidates are re-embedded in batches (the Store exposes no vector read); the dry-run cost estimate covers **both** embedding and LLM resolution calls.
- **Dry-run by default**: prints planned ops, per-op counts, and the cost estimate; `--apply` executes. Batches are applied incrementally; a mid-run failure leaves prior batches applied and reports the resume offset.
- **Cloud auth**: on the cloud tier the script reuses the `mcp/tenancy` GoTrue login flow (MCP user creds from `.env`) to obtain `access_token` + `org_id` before `get_store()` — it does not run unauthenticated against RLS.

Net effect: every NOOP/UPDATE/SUPERSEDE decision reduces live rows by one; `--apply` shrinks `count_findings`, and no provenance URL leaves the live row set **except via SUPERSEDE** — a contradicted finding's URLs retire with it by design (§C2) and stay reachable through history. Both invariants are evaluated over **live rows**.

### C6 · SupabaseStore parity (new — the branch predates it)

Implement on `delapan/store/supabase.py`, mirrored by fake-supabase tests in the style of `tests/test_supabase_findings.py`:
- The three §C2 write primitives: `update_finding` (None→keep contract as defined in §C2 Notes), `invalidate_finding` (plain column update under RLS), and `supersede_finding` — via a `supersede_finding` RPC doing insert+invalidate atomically server-side. The wrapper builds `p_row` with the **same client-side enrichment as `insert_findings`** (org_id, `status='approved'`, created_at, valid_from, embedding as vector text); the RPC body performs the explicit casts (`::uuid`, `::timestamptz`, `::vector`).
- `insert_resolution_events` (best-effort: failures logged, never raised) and `list_resolution_events`.
- `valid_from`/`invalidated_at`/`superseded_by` in the row mapping; `list_findings(include_invalidated=False)`; live-only `count_findings`.
- `match_findings` needs **no client change** — the exclusion is server-side in the RPC.

Until C6 lands **and** the migration is applied+verified, the §Architecture cloud guard (pure-ADD when `active_backend() == "cloud"`) keeps resolution off the cloud tier regardless of `memory.enabled` (§Rollout step 5 removes it).

## Migration (Supabase)

```sql
-- valid_from: add WITHOUT default first (Postgres fills a default into all existing
-- rows at ALTER time, which would clobber the created_at backfill), then set default.
alter table findings add column if not exists valid_from timestamptz;
update findings set valid_from = created_at where valid_from is null;
alter table findings alter column valid_from set default now();

alter table findings
  add column if not exists invalidated_at timestamptz,
  add column if not exists superseded_by uuid references findings(id) on delete set null;
create index if not exists findings_live_idx on findings (kb_id) where invalidated_at is null;

create table if not exists resolution_events (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  kb_id uuid not null,
  op text not null,
  candidate_title text not null,
  target_finding_id uuid,
  new_finding_id uuid,
  details jsonb,
  reason text default '',
  created_at timestamptz default now()
);
alter table resolution_events enable row level security;
-- Policies: copy the findings table's org-scoped select/insert policies verbatim
-- (dump with pg_get_policydef equivalents at apply time), renamed for this table.
```

**RPC changes** — the deployed `match_findings` body exists only on the live instance: at apply time, dump it with `pg_get_functiondef`, add `and f.invalidated_at is null` to its WHERE clause, record the final SQL in this spec's implementation PR, then `create or replace`. Add a new `supersede_finding(p_kb_id, p_target_id, p_row jsonb) returns uuid` RPC that inserts the new row and updates the target in one transaction.

## Error handling

- Resolver LLM failure / malformed structured output → ADD-all fallback (existing).
- Store failure applying one op → log, continue with remaining candidates; events record applied ops. A failed `supersede_finding` rolls back atomically and logs as **failed** with the target id and error — no orphaned rows exist under the transactional contract.
- `insert_resolution_events` stays best-effort (never blocks persist) — on cloud this means wrapped, not absent (§C6).
- Backfill `--apply`: per-batch application with resume offset on failure.

## Testing & acceptance

1. All branch tests + new unit tests green; existing suite unaffected.
2. Golden sets pass offline in CI (no network).
3. **Idempotence proof**: seed a temp KB from a fixture, run the same candidate batch twice through `resolve_and_persist`. Second pass: ~0 ADDs **and ~0 UPDATE/SUPERSEDEs** (NOOP-dominant, ≥90%; ≤10% tolerated for LLM nondeterminism), live `count_findings` stable, and total row growth ≤ the tolerated UPDATE noise. Pass-2 invalidations are bounded by that same noise and each must leave a live `superseded_by` successor. NOOPs with no new URLs leave findings rows byte-identical (their events still log).
4. Superseded rows never appear in `match_findings` output (both tiers), and `delapan_resume` / search reflect that.
5. Kill-switch: `memory.enabled: false` reproduces pure-ADD behavior — rows identical to pre-change master apart from the three new columns.
6. Bands recalibrated for `gemini-embedding-001`; the reproduced off-topic-rich golden case now returns `sparse`/`gap`.
7. Backfill dry-run on `delapan/master` (contains the 2026-07-15 48-finding enrichment) reports a plausible op mix with self-matches excluded; `--apply` on a **copy** reduces live `count_findings`, and every provenance URL present before remains on some **live** row except URLs whose only holders were SUPERSEDE-retired (each such URL must remain on the invalidated row for history).

## Out of scope

Hybrid FTS candidate retrieval and reranking (E3 — the resolver's candidate stage is embedding-only for now, by design), gap flywheel / `record_access` (E4), deepen wiring and `search_mode: agent` (E5), sleep-time consolidation (E6), KG repositioning (E7), docs reconciliation beyond what this change touches.

## Rollout

1. Merge `feat/mem0-resolution-port` → master **dark**: the merge commit flips `memory.enabled: false` in `config.yaml` *and* the `MemoryConfig` default (the branch ships both as `true`). C1 resolver-prompt update rides along; `fix/finding-content-roundtrip` is **not** merged (redundant, conflicts — cherry-pick its regression test only).
2. Land C2 (op remap, write primitives incl. `supersede_finding`/`invalidate_finding`, the **cloud pure-ADD guard**) + C3 (columns, live-only filters/counts) with unit tests.
3. Land C4 harness; recalibrate bands; commit new thresholds. Flip `memory.enabled: true` — the step-2 guard keeps cloud on pure ADD, so this enables resolution on the **local** tier only.
4. Land C6 (SupabaseStore methods + fake tests).
5. Apply the Supabase migration + RPC changes; verify cloud-tier parity (acceptance #4–#5 against a cloud KB); then **remove the cloud pure-ADD guard** — that removal is what "enable memory on cloud" means.
6. Backfill `delapan/master` (dry-run → review → apply), then other noisy KBs (`delapan/dev`) as desired.
