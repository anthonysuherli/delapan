# Design: delapan-fe becomes `delapan.ai` — auth-gated, real data, invite-only beta

**Date:** 2026-07-24
**Status:** Approved (design), pending implementation plan
**Repos touched:** `delapan-fe` (frontend), `delapan` open-core engine (backend `/api` deploy), Vercel + Fly (infra)

**Vision goals served:** *"Hosted public tier with account isolation"* — an
account-gated dashboard on delapan.ai backed by an authenticated engine API,
launched as a free invite-gated beta. Also realizes the Non-Goal *"Not a
greenfield dashboard. We evolve the existing sigma.js control panel"* — delapan-fe
**is** that panel; putting it on the domain aligns deployment with the vision.
Honors the invariant *cloud tier authenticates via Supabase JWT with org-scoped
RLS*; the local open-core tier stays auth-less (unchanged).

## Problem

`delapan.ai` is currently served by the Next.js monorepo frontend
(`anthonysuherli/delapan-ai` `frontend/`, Vercel project `delapan`). We want the
**sigma.js instrument-panel dashboard** (`delapan-fe`) to be the face of
delapan.ai instead — gated behind Supabase login, showing real KB data, as an
invite-only beta.

This is **not** a frontend-only deploy. Three facts drive the scope:

1. delapan-fe consumes the open-core engine's per-KB REST contract
   (`/api/projects/{p}/kbs/{k}/graph|nodes|edges|findings|synopsis|resume|explore`),
   defined in `delapan-fe/src/api/client.ts`. Today it sends **no `Authorization`
   header**.
2. That contract is served only by the open-core `delapan.api.main` FastAPI app —
   which is **not deployed anywhere**. The Fly app `delapan-cloud-mcp` runs
   `delapan.mcp.cloud_server` (the MCP server), not the REST app. The monorepo
   Railway backend serves a **different, incompatible** contract (`/v1/findings`,
   `/v1/graph` — flat public reads), so it cannot back delapan-fe.
3. `delapan.api.main` already mounts every router delapan-fe needs behind an auth
   dependency, and `delapan/api/auth.py` already verifies Supabase **ES256 JWTs
   via JWKS** (`PyJWKClient`) — resolving the HS256-only rejection gate. The
   capability exists; it just isn't running.

## Decisions (ratified via brainstorming Q&A, 2026-07-24)

- **Replace** delapan.ai with delapan-fe (Next.js frontend stops serving the
  domain; its repo stays intact).
- **Auth-gate first** — Supabase login in front of the graph before any public
  deploy.
- **Deploy the open-core `/api` app** as a new backend service (not the monorepo
  Railway backend, not mock).
- **Invite-only beta** — delapan.ai becomes a login-gated dashboard only. Public
  landing, self-serve `/signup`, and `/privacy` + `/terms` are **out of scope**
  this pass.

## Architecture — three workstreams

### A · Frontend auth gate (`delapan-fe`)

- **Session gate around the whole SPA.** Reuse the existing `/tracking` Supabase
  client (`src/tracking/supabaseClient.ts`). On boot: `getSession()` → no session
  renders a login screen (reuse the `/tracking` GoTrue login form); a valid
  session renders the app. Subscribe to `onAuthStateChange` to flip between the
  two states.
- **Attach the JWT to every call.** In `src/api/client.ts`, the single `http()`
  helper **and** the two SSE fetches (`liveExplore`, canvas search) gain
  `Authorization: Bearer <access_token>`, read fresh from `getSession()` per
  request (supabase-js auto-refreshes — never cache a stale token).
- **401 handling.** A `401` `ApiError` → sign out → login screen. Preserve the
  existing network-error → mock fallback for genuine outages only (a 401 must not
  silently drop to mock).
- **Env:** `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` (already used by
  `/tracking`), `VITE_API_BASE` → the new backend, `VITE_USE_MOCK` unset in prod.

### B · Backend: deploy the open-core `/api` app

- **New Fly app** (e.g. `delapan-api`) from `~/projects/delapan`, reusing the
  existing Dockerfile with the CMD overridden to run `delapan.api.main` via
  uvicorn instead of `delapan.mcp.cloud_server`. Isolated from the MCP app; same
  secrets / Supabase wiring. (Alternative considered: a Fly `[processes]` group in
  the existing app — rejected for weaker isolation and coupled scaling.)
- **Config (all via `config.yaml` / `DLP_*` env, never hardcoded):**
  `api.auth = "supabase"` + `supabase_url` set (drives the ES256/JWKS path in
  `auth.py`); `Store` → shared cloud Supabase project `gunqbyddzuwzpncfigro`.
- **CORS** allow-list the `delapan.ai` origin. Keep `ratelimit.py` enabled.
- **Invite/beta gate** — confirm `auth.py`'s beta gating enforces invite-only, or
  enforce at the Supabase allowlist. (Settle exact point in the plan.)

### C · Domain cutover (Vercel)

- Vercel project for the `delapan-fe` repo — Vite preset, build `npm run build` →
  `dist/`, **SPA catch-all rewrite** (today `vercel.json` rewrites only
  `/tracking` + `/duet`; the app needs all-paths → `index.html`). Set the env vars
  from A.
- Move the `delapan.ai` domain **off** the Next.js project **onto** the delapan-fe
  project. Reversible — moving it back restores the current site.

## Sequencing (each stage independently reversible)

1. **A** → deploy delapan-fe to its `*.vercel.app` on **mock** (`VITE_USE_MOCK=1`):
   proves login + build pipeline with zero data exposure.
2. **B** → deploy `/api`, point `VITE_API_BASE` at it, verify real data behind
   auth on the preview URL.
3. **C** → move the `delapan.ai` domain. Rollback = move it back to the Next.js
   project.

## Out of scope (this pass)

Public landing page, self-serve `/signup`, `/privacy` + `/terms`, custom SMTP for
auth email, and the rest of the vision's public-launch hardening — deferred while
invite-only. The Next.js frontend repo stays intact (just stops serving the
domain), so nothing is lost and the cutover is reversible.

## Risks / details to settle in the implementation plan

- **delapan-fe routing model** (history vs. hash) → exact Vercel SPA rewrite.
- **Existing Vercel project?** — check whether a delapan-fe Vercel project already
  exists before creating one.
- **Uvicorn CMD / port / healthcheck** for the Fly API app (`internal_port`,
  `/health`).
- **Invite/beta enforcement point** — backend `auth.py` gate vs. Supabase
  allowlist.
- **RLS coverage** — every KB/finding/graph table the `/api` app reads must have
  org-scoped RLS verified (run `scripts/rls_audit.py`) so a logged-in beta user
  cannot read another org's data. This is the load-bearing invariant for going
  public.
- **CORS + cookie/session** interplay between the Vercel origin and the Fly API.

## Acceptance criteria

- Logged-out visit to `delapan.ai` shows the login screen; no KB data reachable.
- A logged-in beta user sees the sigma dashboard populated with **real** data from
  the deployed `/api` backend (not mock), with every API call carrying the
  Supabase JWT.
- A second account cannot read the first account's projects/KBs/findings/graph
  (RLS-verified, not by inspection).
- Rolling the `delapan.ai` domain back to the Next.js project restores the prior
  site with no data loss.
