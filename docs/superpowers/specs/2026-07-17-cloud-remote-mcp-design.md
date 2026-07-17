# Cloud-hosted remote MCP server for claude.ai — design spec

- **Date:** 2026-07-17
- **Status:** approved design, ready for implementation planning
- **Repo:** `delapan` (backend; branch `docs/cloud-remote-mcp-spec` off `master`, symlinked to `~/projects/delapan`)
- **Topic:** Make delapan's four MCP tools (`delapan_resume`, `delapan_search`, `delapan_explore`, `delapan_projects`) reachable from claude.ai (web/desktop), not just Claude Code's local stdio spawn.

## 1. Summary

Today the MCP server (`delapan/mcp/server.py`) only runs over stdio, spawned
locally by Claude Code. claude.ai's custom connectors require a server
reachable over HTTPS from Anthropic's infrastructure, speaking MCP over
`streamable-http`, with real authentication (this data is private).

The storage side of this is already solved: `SupabaseStore` (see
[2026-06-22-supabase-store-cloud-tier-design.md](2026-06-22-supabase-store-cloud-tier-design.md))
is merged to `master`, with `org_id`-scoped RLS and a working GoTrue-login →
user-JWT → `get_store(token, org_id)` path in `mcp/tenancy.py`. This project
adds the missing piece: a network-reachable **resource server** in front of
that store, authenticated via Supabase Auth's own OAuth 2.1 server (public
beta, already available on the same Supabase project), deployed somewhere
always-on. It changes no engine call sites and does not modify the existing
local stdio server.

## 2. Background — what exists vs what's missing

**Already done:**
- `SupabaseStore`, `mcp/tenancy.py`'s cloud branch (GoTrue login, `org_id`
  resolution, RLS-scoped `get_store`) — merged to `master`.
- The `actuary` project (4 KBs) is live on cloud Supabase, org
  `1a7d0aa5-587f-4420-985b-bafcf03bf04f`, project `d3df020b-…`.
- `scripts/port_actuary_to_cloud.py` — a proven local→cloud migration script
  (dry-run default, `--execute`, `--rollback <project_uuid>`), currently
  hardcoded to `PROJECT_NAME = "actuary"`.
- Supabase Auth's OAuth 2.1 server (DCR, PKCE, RLS-integrated tokens,
  standard `/.well-known/oauth-authorization-server` discovery) — public
  beta since 2025-11-26, available on the same Supabase project with no
  separate infrastructure to stand up.

**Missing (this build):**
- Any transport other than stdio for the MCP tools. `mcp.run()` in
  `delapan/mcp/server.py` takes no transport argument (defaults to stdio);
  the underlying FastMCP library supports `streamable-http` but it's unused.
- Any non-loopback deployment. `delapan/api/main.py`'s FastAPI app binds
  `127.0.0.1` only; there is no Dockerfile, `fly.toml`, or other deploy
  config anywhere in the repo.
- A resource-server auth handshake (protected-resource metadata, spec-correct
  `401`/`WWW-Authenticate`) in front of the MCP tools.
- Cloud copies of the two remaining local-only projects: `demo/main` (28
  findings) and `qwen-hackathon/main` (24 findings) — verified against the
  live `~/.delapan/delapan.db` on 2026-07-17. (`actuary/unified` has 0
  findings locally; nothing to migrate there.)

## 3. Scope

**In scope:**
- New entrypoint `delapan/mcp/cloud_server.py`: the four existing MCP tools
  over `streamable-http`, forced `DELAPAN_BACKEND=cloud`.
- Resource-server auth glue: `/.well-known/oauth-protected-resource`,
  `401`/`WWW-Authenticate` handshake, passing the Supabase-issued access
  token into the existing `tenancy.py` path unchanged.
- Enabling Supabase's OAuth 2.1 server on project `d3df020b-…` and
  registering/relying on DCR for the claude.ai client.
- Generalizing `port_actuary_to_cloud.py` to take `--project <name>` instead
  of a hardcoded constant; running it for `demo` and `qwen-hackathon`.
- Deploy config (Dockerfile, `fly.toml`) for an always-on host on Fly.io.
- Tests per §9.

**Out of scope (this project):**
- Any changes to `delapan/mcp/server.py` (the local stdio entrypoint) or
  `delapan/api/main.py` (the loopback FastAPI app) — both untouched.
- A specific "invite my brother" onboarding flow. This design builds the
  *mechanism* (anyone who is a Supabase user + `org_members` row can connect
  their own claude.ai and get RLS-scoped access); adding a specific person
  to a specific org is a follow-up data change, not code.
- Fixing `delapan_explore`'s silent-empty-on-Tavily-quota bug (pre-existing,
  tracked separately) — it will behave the same way on the cloud deployment.
- br8n's separate MCP server — not touched by this project.
- Migrating any KB not found in the live local SQLite db as of 2026-07-17
  (see §2) — if other KBs mentioned in older notes exist elsewhere, they are
  out of scope until located.

## 4. Decisions (from brainstorming)

| # | Decision | Rationale |
|---|---|---|
| Auth | **Supabase's built-in OAuth 2.1 server** (not a bespoke authorization server) | Already running on the same project; DCR means claude.ai self-registers; avoids building/maintaining PKCE, token issuance, refresh-rotation from scratch. |
| Auth scope | Build the sharing **mechanism** (org-scoped OAuth), not a specific invite flow | Matches actual near-term need (personal use, possible future sharing) without speculative onboarding UI. |
| Store access | Keep the **existing user-JWT + RLS path** in `tenancy.py` unchanged | Approach considered and rejected: service-role key bypass — faster to build but throws away org scoping for a single always-on credential with full-database reach. Not worth it. |
| Transport | `streamable-http` (FastMCP's built-in support), new sibling entrypoint | Current MCP standard; leaves the local stdio server untouched. |
| Hosting | **Fly.io**, single always-on machine | Cheap, simple Docker+secrets fit for one small always-on process. Swappable for Railway/Render without touching the app code. |
| Migration scope | Generalize `port_actuary_to_cloud.py` (`--project` arg) rather than a new script | Small, well-understood change to a script that's already proven correct for this exact operation. |

## 5. Architecture & files

**Correction (found during planning, 2026-07-17):** the installed `mcp` SDK
(v1.28.1, already a dependency) implements RFC 9728 protected-resource
metadata and the `401`/`WWW-Authenticate` handshake natively —
`FastMCP(auth=AuthSettings(...), token_verifier=...)`. Nothing hand-rolled.
Also, `tenancy.py`'s `resolve_tenant`/`resolve_store` always call the
private `_login()` (password grant for the fixed configured MCP user) —
they do not accept an externally supplied token. Two small additive
functions are needed so the cloud server can pass through the token
claude.ai actually presents (which represents whichever real person
authenticated, not always the fixed MCP user). The **existing** functions
are untouched — this is an addition, not a modification of call sites that
already work.

```
claude.ai ──HTTPS/streamable-http──▶ delapan/mcp/cloud_server.py  (NEW, Fly.io)
                                          │ FastMCP(auth=AuthSettings(...), token_verifier=SupabaseTokenVerifier())
                                          │   → 401/WWW-Authenticate + protected-resource metadata: built into the SDK
                                          └─ verify_token(bearer) ──▶ cloud_auth.SupabaseTokenVerifier  (NEW)
                                                 │ user_client(token).auth.get_user(token) → user_id
                                                 ▼
                                          mcp/tenancy.py: resolve_tenant_for_token / resolve_store_for_token  (NEW, additive)
                                                 │ _org_for(user_id) → org_id   [existing helper, reused]
                                                 ▼
                                          get_store(token, org_id) ──▶ SupabaseStore  (EXISTS)
                                                                            │
                                                                            ▼
                                                          Supabase: Postgres/pgvector (RLS)
                                                                  + OAuth 2.1 server (auth, host = SUPABASE_URL)
```

| File | Change | Responsibility |
|---|---|---|
| `delapan/mcp/cloud_server.py` | new | `streamable-http` entrypoint; imports the four tool functions from `delapan/mcp/server.py`; configures `FastMCP(auth=AuthSettings(...), token_verifier=...)`. |
| `delapan/mcp/cloud_auth.py` | new | `SupabaseTokenVerifier(TokenVerifier)` — one `verify_token()` method that resolves a bearer token to a Supabase user via `auth.get_user()`. |
| `delapan/mcp/tenancy.py` | **modify (additive)** | Add `resolve_tenant_for_token(access_token, project, kb, *, create)` and `resolve_store_for_token(access_token)` — same body shape as `resolve_tenant`/`resolve_store`'s cloud branch, but skip `_login()` and take `(user_id, token)` from the caller. Existing functions unchanged. |
| `scripts/port_actuary_to_cloud.py` | modify | Replace hardcoded `PROJECT_NAME = "actuary"` with a `--project` argument. Rename script if a generic name is clearer (implementation detail, not decided here). |
| `Dockerfile`, `fly.toml` | new | Build + deploy config for the cloud server. |
| `delapan/mcp/server.py`, `delapan/api/main.py` | **unchanged** | Local stdio server and loopback API stay exactly as they are. |

## 6. Auth flow detail

1. claude.ai sends an unauthenticated MCP request to the cloud server. The
   `mcp` SDK's built-in resource-server support returns the spec-correct
   `401` + `WWW-Authenticate` with a `resource_metadata` pointer — no
   hand-written endpoint.
2. claude.ai fetches the protected-resource metadata (also SDK-served); it
   lists Supabase's issuer (`{SUPABASE_URL}/auth/v1`, e.g.
   `https://gunqbyddzuwzpncfigro.supabase.co/auth/v1` — the actual project
   host from `.env`, **not** any internal delapan project UUID) as the
   authorization server.
3. claude.ai discovers Supabase's OAuth metadata, dynamically registers
   itself as a client (DCR — no manual per-org credential step), and drives
   the user through Supabase's real consent screen.
4. Supabase issues an access token (and refresh token) to claude.ai.
5. Every subsequent MCP request carries that token as a bearer header. The
   SDK calls `SupabaseTokenVerifier.verify_token(token)`, which resolves the
   Supabase user via `auth.get_user()`. The tool functions then call
   `resolve_tenant_for_token(token, project, kb)` /
   `resolve_store_for_token(token)`, which reuse the existing `_org_for()`
   lookup and `get_store(token, org_id=...)` — RLS scopes every query to
   whatever org(s) that Supabase user belongs to via `org_members`, exactly
   as it already does for the actuary port today.
6. Token refresh is claude.ai's responsibility (reactive on `401`, proactive
   ~5 minutes before expiry per Anthropic's connector spec) as long as the
   server keeps returning spec-correct `401`s on expired tokens — handled by
   the SDK as long as `verify_token` correctly returns `None` on an expired
   token.

Sharing with another person (e.g. a second Supabase user) requires only an
`org_members` row for them on whichever org's KBs they should see — no code
change. Not executed as part of this project (see §3 scope).

## 7. Migration (local → cloud)

`scripts/port_actuary_to_cloud.py` changes from a single hardcoded project
to a `--project <name>` argument; the dry-run-default/`--execute`/`--rollback`
behavior and UUID-remapping logic are otherwise reused as-is. Run for:

- `demo` (KB `main`, 28 findings)
- `qwen-hackathon` (KB `main`, 24 findings)

Both dry-run first, then `--execute`, matching the actuary port's precedent.

## 8. Deployment

- `delapan/mcp/cloud_server.py` forces `DELAPAN_BACKEND=cloud` — there is no
  local disk on Fly to fall back to, so no tier-detection branch is needed
  here (unlike the local dev entrypoints).
- Secrets (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
  LLM + Tavily keys for `delapan_explore`) live in Fly's secret store, never
  baked into the image or committed.
- Single always-on machine — no autoscaling-to-zero, since MCP connections
  from claude.ai expect a live server, not a cold start.

## 9. Error handling

- Auth failures → `401` + `WWW-Authenticate`, never an MCP tool-call error
  (this is what makes claude.ai's OAuth/refresh flow trigger correctly
  instead of surfacing a confusing failure inside a tool result).
- Supabase/network errors → propagate as real MCP tool errors, matching
  `SupabaseStore`'s existing contract (see the cloud-tier design spec §9) —
  no silent partial results.
- `delapan_explore` on a Tavily-quota-exceeded key still returns a silent
  empty success (pre-existing bug, out of scope — see §3).

## 10. Testing

1. **Unit tests** for `cloud_auth.SupabaseTokenVerifier.verify_token()` (valid
   token → `AccessToken` with the right `subject`; invalid/expired token →
   `None`) and for `tenancy.resolve_tenant_for_token`/`resolve_store_for_token`
   — using the existing `tests/fake_supabase.py` in-memory harness pattern.
   The `401`/metadata handshake itself is the SDK's own tested behavior, not
   re-tested here.
2. **Env-gated live smoke test** (same pattern as `tests/test_supabase_live.py`,
   e.g. `RUN_CLOUD_TESTS=1`): hits the deployed Fly instance end-to-end once
   it's up — a real OAuth token, one call to each of the four tools against
   a migrated KB.
3. **Manual acceptance gate:** add the connector by URL in claude.ai's
   Settings → Connectors UI, complete the consent screen, and run
   `delapan_resume`, `delapan_search`, `delapan_projects`, and
   `delapan_explore` at least once each against the `demo` KB.

## 11. Risks & prerequisites

- **Supabase's OAuth 2.1 server is public beta** (since 2025-11-26). DCR/CIMD
  behavior could still change; there's an open upstream discussion
  ("CIMD Support Soon?") suggesting this surface is still evolving. Revisit
  if Supabase ships breaking changes before this is built.
- **`static_headers` (a simpler bearer-token-only auth mode) exists in beta
  on Anthropic's side** but is documented in terms of an "organization
  administrator" entering the credential — unclear if it applies cleanly to
  a personal (non-Team/Enterprise) claude.ai account. Not chosen here since
  the OAuth path was preferred anyway (for sharing), but worth knowing as a
  fallback if Supabase's OAuth beta proves unworkable.
- **Anthropic's egress range must reach the deployed server and Supabase**
  (`160.79.104.0/21`) — shouldn't be an issue on Fly's public network, but
  confirm no WAF/firewall blocks it.
- **Fly.io account/billing** — not yet set up for this repo; needed before
  deployment.
- **`delapan_explore`'s Tavily-quota bug** (pre-existing) will make explore
  look broken on the cloud deployment too until fixed separately.

## 12. Open questions

None blocking. Deferred: the actual "invite my brother" `org_members` change
(§3); whether to keep `static_headers` as a documented fallback path if the
Supabase OAuth beta breaks; graduating this beyond personal/small-group use
(the `/v1/*` cloud product surface referenced in the vision doc) if that
becomes a real goal later.
