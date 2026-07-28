# SupabaseStore — cloud-tier Store implementation — design spec

- **Date:** 2026-06-22
- **Status:** approved design, ready for implementation planning
- **Repo:** `delapan` (backend; branch `master`, symlinked to `~/projects/delapan`)
- **Topic:** Implement the cloud-tier `Store` (`SupabaseStore`) so the engine + frontend OKF reader can serve KBs (starting with the ported `actuary` project) from Supabase instead of local SQLite.

## 1. Summary

The delapan engine already calls `get_store()` / `resolve_tenant()` behind a
`Store` protocol with two tiers — local SQLite and cloud Supabase. The **cloud
tier dead-ends at missing modules**: `delapan/store/supabase.py`,
`delapan/core/clients/supabase.py`, and `scripts/seed_dev.py` do not exist, and
the `supabase` SDK is not installed. This build fills exactly those gaps and
**changes no engine call sites**. The result: with `DELAPAN_BACKEND=cloud` and
valid creds, the same FastAPI surface the frontend already consumes serves the
cloud KBs.

Implements the **full** `Store` protocol (~30 methods) via the **`supabase-py`**
client (PostgREST + deployed RPCs), authenticated as a **GoTrue user with
RLS** doing org isolation.

## 2. Background — what exists vs what's missing

**Already done (verified against the live instance `<project-ref>`):**
- The cloud Postgres schema is deployed: `projects, kbs, findings, kg_nodes,
  kg_edges, kb_synopsis, explorations, kg_schemas, orgs, org_members`, plus
  cloud-only tables (`access_*`, `api_keys`, `chat_*`, `kg_communities`, …).
- Vector-search **RPCs are deployed**: `match_findings`, `match_kg_nodes`,
  `ensure_default_workspace`, `rollup_access_events`.
- Embeddings are **inline `vector(1536)` columns** on `findings`/`kg_nodes` —
  there are **no `vec_*` tables** in cloud (those are SQLite-only).
- The `actuary` project (4 KBs, 160 findings, 264 nodes, 259 edges, embeddings)
  is ported under org `<org-uuid>` (which has 2 owner
  members). New cloud project id `d3df020b-5085-41c0-a561-490a202baa23`.
- `tenancy.py`'s cloud branch already does GoTrue login (`_login`) + org lookup
  (`_org_for`) and calls `get_store(token, org_id)`.

**Missing (this build):**
- `delapan/store/supabase.py` — `SupabaseStore` (the protocol impl).
- `delapan/core/clients/supabase.py` — `service_client()` / `user_client(token)`
  factories (`tenancy.py` already imports `service_client` from here).
- `scripts/seed_dev.py` — provision/ensure the MCP GoTrue user + `org_members`.
- The `[cloud]` extra is declared in `pyproject.toml` but not installed.

## 3. Scope

**In scope** — the full `Store` protocol (all ~30 methods in `store/base.py`):
findings (incl. `match_findings`, `insert_findings`), synopsis, exploration
lifecycle, tenancy (`resolve_project`/`resolve_kb`/`list_projects`), the activity
KG (upsert/update/delete/match/subgraph/stats/list/get/clear), KG intent schema,
init/drift stamps, and `record_access`. The client factories, the seed script,
installing `[cloud]`, and the `.env`/config wiring.

**Out of scope (future):**
- Atomic upsert RPCs for the read-then-write methods (see §7); MVP accepts the
  documented non-atomicity under single-user load.
- Authoring/altering the cloud schema or RLS policies (assumed deployed; this
  build **verifies** RLS, it does not create it).
- Migrating more data (the `actuary` port is already done).
- Switching the default tier; `DELAPAN_BACKEND` stays `local` until verified.

## 4. Decisions (from brainstorming)

| # | Decision | Rationale |
|---|---|---|
| Scope | **Full protocol** | Enables explore/ingest on cloud too, not just serving. |
| Auth | **User-JWT + RLS** | Production-faithful; matches existing `tenancy.py`. |
| Transport | **`supabase-py`** (PostgREST + RPC) | The only path where the user JWT + RLS + the deployed RPCs do the work; mirrors `tenancy.py`'s `create_client`. asyncpg would bypass RLS and re-author vector SQL. |
| Async | Async protocol methods wrap the sync client in `asyncio.to_thread` | supabase-py is synchronous; avoids blocking FastAPI's loop. |
| Embeddings | Inline `vector(1536)` columns, written as a `"[…]"` string | Cloud has no `vec_*` tables; pgvector text input is the portable form. |
| MCP user | Default: a small `seed_dev.py` provisions a dedicated MCP user + org membership | Avoids sharing an owner password; overridable with existing owner creds. |

## 5. Architecture & files

No engine call sites change. The cloud branches that already exist simply stop
dead-ending.

```
request → route → resolve_kb_or_404 → resolve_tenant (cloud branch, EXISTS)
            │                              │ _login()  GoTrue → user JWT
            │                              │ _org_for() service-role → org_id
            ▼                              ▼
        get_store(token, org_id) ──► SupabaseStore  (NEW)
                                         │ user-scoped supabase-py client (RLS)
                                         ├─ .table(...)  CRUD  (PostgREST)
                                         └─ .rpc("match_findings"/"match_kg_nodes")
```

| File | Change | Responsibility |
|---|---|---|
| `delapan/core/clients/supabase.py` | new | `service_client()` (service-role) + `user_client(token)` (user-JWT) factories over `supabase.create_client`. |
| `delapan/store/supabase.py` | new | `SupabaseStore(access_token, org_id)` — full protocol over the user-scoped client. One file, mirroring `sqlite.py`. |
| `scripts/seed_dev.py` | new | Provision/ensure the MCP GoTrue user + its `org_members` row (service-role). Idempotent. |
| `pyproject.toml` | install | `pip install -e ".[cloud]"` (extra already declares `supabase`, `asyncpg`, `pyjwt`, `argon2`). |
| `backend/.env` | config | `SUPABASE_URL`, `SUPABASE_ANON_KEY` (=publishable), `SUPABASE_SERVICE_ROLE_KEY` (=secret), `DLP_MCP_USER_EMAIL`, `DLP_MCP_USER_PASSWORD`. (`SUPABASE_JWT_SECRET` is dead config — skipped.) |

**Boundary discipline:** `supabase` stays a lazy, cloud-only import so a
local-only install never needs the `[cloud]` extra (matches `tenancy.py`).

## 6. Cross-cutting rules (apply to every method)

1. **Embeddings** — send as a bracketed string `"[f1,…,f1536]"` (never the
   SQLite binary blob, never a JSON list). Exactly 1536 elements; pgvector
   rejects a length mismatch.
2. **`org_id` is `NOT NULL`** on every table — inject the store's `org_id` into
   every INSERT. (SQLite's synthetic `"local"` org does not carry over.)
3. **Reads scope by `kb_id` only** — never add an explicit `org_id` filter on
   reads; the user JWT + RLS scope to the org.
4. **Vector RPCs** — `.rpc("match_findings"/"match_kg_nodes", {query_embedding,
   match_kb_id, match_count, min_similarity})`. The param is **`match_kb_id`**
   (not `kb_id`); the RPC returns a **trimmed** projection (no `org_id`/
   `created_at`/`embedding`), so callers must not expect those back.
5. **JSON/array columns** — `tags`/`aliases` are `text[]` (send Python lists);
   `provenance`/`properties`/`grounded_in`/`merge_history` are `jsonb` (send
   dicts/lists). PostgREST coerces — no manual `json.dumps`.
6. **Column shape** — edges use `source_node_id`/`target_node_id`; `findings`
   has a required `status`; `kg_nodes` has required `aliases`/`merge_history`
   and nullable `community_id`. Fill required cloud-only fields on write
   (`status="approved"`, `aliases=[]`, `merge_history=[]`).

## 7. Method mapping (verified RPC signatures)

Deployed RPCs (live-tested):
- `match_findings(query_embedding text-vector, match_kb_id uuid, match_count int,
  min_similarity real)` → rows `{id,title,content,category,confidence,tags,
  provenance,similarity}`.
- `match_kg_nodes(query_embedding text-vector, match_kb_id uuid, match_count int,
  min_similarity real)` → rows `{id,type,label,properties,similarity}`. A
  zero-vector query returns 0 rows (degenerate cosine) — smoke-test with a real
  embedding.

| Group | Methods | Mechanism |
|---|---|---|
| **Findings** | `match_findings` (rpc), `insert_findings`, `get_finding`, `get_finding_global`, `list_findings`, `delete_finding`, `count_findings` | `.table("findings")` CRUD + the rpc. Single-row writes (inline embedding). `count_findings` uses `count="exact"`. |
| **KG read** | `get_kg_subgraph`, `kg_stats`, `list_kg_nodes`, `get_kg_node`, `match_kg_nodes` | Subgraph **BFS in Python** over PostgREST fetches (no recursive SQL); incident-edge fetch via two queries (source, target) unioned, not a single `.or_()`. `kg_stats` counts via `count="exact"` + client-side `by_type`/`by_relation` aggregation. `match_kg_nodes` rpc. |
| **KG write** | `upsert_kg_nodes`, `upsert_kg_edges`, `update_kg_node`, `delete_kg_node`, `delete_kg_edge`, `clear_kg` | `.table(...)` insert/update/delete. Node dedupe-merge on `(kb_id,type,label)`: select existing → merge (`properties` existing-wins, `grounded_in` order-preserving union capped at 50) → insert/update. Edge dedupe on `(kb_id,source,target,relation)`. `delete_kg_node` fetches incident edges, deletes edges then node. |
| **Tenancy** | `resolve_project`, `resolve_kb`, `list_projects` | Find-or-create by name (read then insert when `create=True`). `list_projects` is an N+1 fetch (projects → kbs → snapshot stats) — acceptable at current scale. |
| **Synopsis / explore / intent / stamps / monitoring** | `load_synopsis`, `upsert_synopsis`, `create_exploration`, `update_exploration`, `get_exploration`, `get_kg_intent`, `set_kg_intent`, `get/mark_init_offered`, `get/set_drift_marker`, `record_access` | Direct `.table(...)` calls. `upsert_synopsis` uses `on_conflict="kb_id"`. `set_kg_intent` computes `next_version = max+1` (read then insert). Stamp getters defensively return `False`/`0` on error. `record_access` is best-effort — never raises. |

### Accepted non-atomicity (single-user MVP)

These are single-transaction in SQLite but become multiple non-atomic PostgREST
calls in cloud. Under one local user the races don't occur; the clean fix later
is deployed upsert RPCs. Documented, not hidden:
- `delete_kg_node` — edges then node, in sequence.
- `upsert_kg_nodes` dedupe-merge — select-then-write per `(type,label)` key.
- `resolve_project` / `resolve_kb` / `set_kg_intent` — read-then-insert race on
  the unique constraint (the constraint is the backstop).

## 8. Data flow

The user JWT rides every PostgREST call, so **RLS does org scoping**. The store
passes `kb_id` filters on reads and never an explicit `org_id` filter; on writes
it sets `org_id` explicitly (NOT NULL). Async methods (`match_*`, `insert_*`,
`upsert_kg_*`, `update_kg_node`, `record_access`) run the sync client under
`asyncio.to_thread`.

## 9. Error handling — match SQLiteStore's contract

- **Not-found:** PostgREST returns empty `.data` (not an exception). Raise the
  same way SQLite does so routes map to **404**: `get_finding`/
  `get_finding_global` raise on empty; `get_kg_node`/`load_synopsis`/
  `get_exploration` return `None`.
- **RLS nuance:** a row the user can't see returns *empty*, indistinguishable
  from absent → surfaces as 404. Correct and acceptable.
- **API/transport errors** (`PostgrestAPIError`, network): propagate; the route
  layer + the frontend's optimistic rollback handle them as today.
- **`record_access`** best-effort — try/except, never raises.
- **Non-atomic writes:** on mid-sequence failure, log and propagate; the
  frontend command rolls back optimistically. No silent partial success.

## 10. Testing

1. **Unit tests with a fake `supabase-py` client** (bulk, hermetic): inject a
   stub client; assert each method issues the right `.table(...)`/`.rpc(...)`
   calls and applies the transforms — `org_id` injection, the `"[…]"` embedding
   encoding, `match_kb_id` rename, JSON/array shapes, BFS/merge logic, the
   not-found→raise vs →`None` contract.
2. **Opt-in live smoke tests** (env-gated `RUN_CLOUD_TESTS=1`): a few read-only
   assertions against the real cloud `actuary` KB — `get_finding`,
   `get_kg_subgraph`, `match_findings` return non-empty — validating the real
   RLS + RPC contract. Skipped by default so CI/local stays hermetic.
3. **Manual E2E (acceptance gate):** `DELAPAN_BACKEND=cloud`, open the OKF reader
   on a cloud `actuary` node; verify frontmatter, findings, related, and a live
   synthesize round-trip.

## 11. Risks & prerequisites

- **RLS is unverified (the gate).** The service-role key bypasses RLS, so it
  could not be confirmed whether `findings/kg_nodes/kg_edges` have RLS enabled
  with `org_id` policies. If RLS is **off** → user-JWT reads leak across orgs
  (security bug); if **on but the MCP user's org doesn't match** the ported
  data → the reader reads **empty**. **The first implementation task verifies
  RLS** (`pg_policies`/`relrowsecurity` via the SQL editor, plus a read with the
  user JWT) before building against it.
- **MCP user must belong to the actuary org.** `seed_dev.py` provisions a
  dedicated user and inserts its `org_members` row for org `1a7d0aa5-…` (or the
  user supplies an existing owner's creds).
- **Credentials** in `.env` before the live/E2E layers: `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `DLP_MCP_USER_EMAIL`,
  `DLP_MCP_USER_PASSWORD`. **Rotate the secret key** that was shared in plaintext
  during the data port.
- **`[cloud]` extra** must be installed in the venv for any cloud code path.

## 12. Open questions

None blocking. Deferred: atomic upsert RPCs (removes §7 non-atomicity); a single
`list_projects` join RPC (removes the N+1); graduating the MCP-user seed into a
first-class onboarding flow.
