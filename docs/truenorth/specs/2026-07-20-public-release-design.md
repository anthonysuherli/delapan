# Public release — hosted tier (design)

> Status: APPROVED — design ratified in session 2026-07-19/20 after the vision
> amendment of 2026-07-19 ("Hosted public tier with account isolation").
> Next step: implementation plan (truenorth writing-plans).

**Vision goals served:** *Hosted public tier with account isolation* (added
2026-07-19) is realized directly; *Two tiers at protocol parity* is served by
keeping the dashboard and `/api` contract tier-agnostic (cloud verifies JWTs,
local stays auth-less).

delapan.ai becomes a public product: a landing page at `/`, an account-gated
dashboard at `/app` on real (non-mock) data, self-serve sign-up behind an
invite allowlist, and the production-hardening minimum a public sign-up link
demands.

```
visitor ──▶ delapan.ai/  (landing: hero · one CTA · waitlist · /demo link)
                │ invite granted
                ▼
        sign-up (email+password | GitHub OAuth, Turnstile, verified email)
                │ handle_new_user ──▶ org + org_members row
                ▼
        /app  (session guard → beta_members gate → org-scoped dashboard)
                │ Bearer <Supabase JWT> on every call
                ▼
        engine /api on Fly  ──▶ TenantContext(org) ──▶ Store ──▶ Supabase (RLS)
```

## Ratified decisions

| Decision | Choice |
|---|---|
| Release path | Hosted SaaS dashboard (Canvas v1 draft unaffected — candidate later release) |
| Sign-in methods | Email+password + GitHub OAuth (Supabase Auth) |
| Launch gating | Free, invite-gated beta; **no billing** (non-goal) |
| Site structure | Landing at `/`, app at `/app`, demo (mock) at `/demo` |
| App data path | Authenticated engine API — one `/api` contract for both tiers |
| Invite gate | Post-auth `beta_members` allowlist (sign-up open, access gated) |
| Tenancy | Org-per-user; multi-member orgs are post-launch (non-goal) |

**Design principle (user directive):** consider free, already-available managed
services before building. Each component below records the use-vs-build call;
"considered, deferred" options are named so they aren't re-litigated.

## What exists today (grounding)

- **Backend** (`~/projects/delapan`): cloud MCP server on Fly
  (`delapan-cloud-mcp`, streamable-http) with Supabase Auth OAuth 2.1 bearer
  verification (`mcp/cloud_auth.py::SupabaseTokenVerifier`), tenancy
  resolution (`mcp/tenancy.py::resolve_tenant_for_token`), `org_members` +
  org-scoped RLS, `handle_new_user` provisioning trigger. The FastAPI REST
  `/api` is loopback/dev-only, auth-less, name-scoped
  (`api/deps.py::resolve_kb_or_404`).
- **Frontend** (`delapan-fe`): Vite/React SPA on Vercel; hand-rolled pathname
  router (`/` dashboard, `/tracking` private Supabase email+password). The
  dashboard has no auth and falls back to bundled mock data on network
  failure — the deployed `/` serves mock data.
- **Missing entirely:** landing page, sign-up, invite gate, authenticated API
  for the dashboard, legal pages, custom SMTP, CAPTCHA, rate limiting, error
  tracking, uptime/status, analytics, OG tags, onboarding empty state.
- **In flight elsewhere (do not touch here):** the stale `OPENAI_API_KEY`
  gate in `api/deps.py::missing_pipeline_keys` is being fixed in a separate
  session.

## A. Architecture

One SPA, one engine process, one auth authority.

- **Vercel** serves the SPA; router grows routes `/`, `/app`, `/demo`,
  `/terms`, `/privacy` (hand-rolled router convention kept; `/tracking`
  unchanged). `vercel.json` rewrites extend to the new paths.
- **Fly.io** keeps the single existing app; the FastAPI REST `/api` is
  mounted alongside the cloud MCP server in the same ASGI process (one
  deploy). Considered, deferred: a second Fly app for the REST tier.
- **Supabase** (`<project-ref>`) is the auth authority + cloud store.
- **Local tier untouched:** same `/api` contract served auth-less on
  loopback; no local workflow requires an account (invariant).

## B. Auth & accounts

Use Supabase Auth built-ins throughout (use-over-build):

- Email+password with **email confirmation on**; password reset; OTP expiry
  ≤ 1h; **GitHub OAuth** via `signInWithOAuth` (new GitHub OAuth app).
- **CAPTCHA:** Cloudflare Turnstile (free, Supabase-native) on
  signup/signin/reset.
- **SMTP:** Resend free tier (3k/mo) as Supabase custom SMTP — replaces the
  2-email/hour built-in default that would silently break public sign-up.
  Considered, deferred: Brevo.
- **Sign-in UI:** hand-rolled screens extending the existing `SignInForm`
  pattern (instrument-panel aesthetic; official auth-ui lib not actively
  developed). supabase-js owns session state; `/app` handles the OAuth
  redirect callback via session detection.
- **Org-per-user:** the existing `handle_new_user` trigger provisions org +
  `org_members` on signup. Verification item: confirm it fires for OAuth
  signups as well as email; extend if not.
- **Invite gate:** `beta_members` table — `user_id uuid` PK referencing
  `auth.users`, `granted_at timestamptz default now()`, `note text`. RLS:
  authenticated users SELECT only their own row; writes via service role
  only. Signed-in non-members see a waitlist screen; granting access is one
  insert. Migration in `backend/migrations/`.
- **Waitlist capture:** Tally form embed on the landing page (free, zero
  backend/abuse surface). Considered, deferred: a Supabase `waitlist` table
  with anon inserts.

## C. Authenticated engine API

A FastAPI auth dependency (`api/auth.py::verify_bearer`) verifies the bearer
token locally — no GoTrue round-trip per request — then hands off to the same
tenancy resolution the cloud MCP server uses: `Authorization: Bearer <Supabase
JWT>` → `verify_bearer` (ES256 against the project's JWKS via a cached
`PyJWKClient`; HS256 against `SUPABASE_JWT_SECRET` as a legacy fallback for
self-hosted projects, never the primary path) → `resolve_tenant_for_token` →
`TenantContext` → org-scoped `Store` calls. This is a separate local verifier
from the cloud MCP server's own `SupabaseTokenVerifier` (`mcp/cloud_auth.py`,
a network `auth.get_user()` call) — same tokens, different transport, kept
distinct deliberately so `/api` stays fast under load.

- **Config, never hardcoded:** `api.auth: none | supabase` in `config.yaml`
  (`DLP_API__AUTH`), default `none` — the local tier's behavior is
  byte-identical to today, and `api.auth` is a `Literal` so a typo raises
  loudly at boot rather than silently falling into the auth-less branch.
  Rate-limit knobs live beside it. `api.cors_origins` is **not yet
  delivered** — CORS still reads `get_settings().cors_origins` (env-only,
  `CORS_ORIGINS`) unioned with hardcoded localhost:5173 dev origins
  (`delapan/api/main.py`); deferred to phase 2.
- **Tenancy resolution:** cloud path resolves project/KB names within the
  token's org (the loopback name-scoped `resolve_kb_or_404` remains the
  `auth: none` path). Mostly no tenant creation over HTTP: the one deliberate
  exception is `/explore` (`request_tenancy_creating`), which may create the
  project/KB on demand — §D's guided first explore needs it. Every other
  route (canvas, findings, graph, projects) stays non-creating, as does the
  entire local (`auth: none`) path.
- **Enforcement line:** the engine's Store implementations are NOT uniform
  here — `SupabaseStore.__init__` (`delapan/store/supabase.py`) uses
  `user_client(access_token)` (anon key + the caller's own JWT), so ordinary
  reads/writes through `Store` are fully RLS-scoped: **RLS is the primary
  wall on the data path**, which makes the `WITH CHECK` migrations and
  `scripts/rls_audit.py` load-bearing, not defense-in-depth. (One read
  depends entirely on the SELECT policy with no additional narrowing:
  `store.get_finding_global(finding_id)` filters by id only.) The service
  client (RLS-bypassing) appears only in two narrow, explicitly-filtered
  spots outside `Store` proper — `tenancy._org_for` and
  `auth.require_beta` — both scoped with an explicit `.eq("user_id", ...)`.
  Both layers are tested (§H).
- **Rate limiting:** slowapi in-process, keyed user-id then IP, budgeted per
  route group (auth-adjacent, reads, pipeline actions), enforced through two
  separate mechanisms — the `@limiter.limit(...)` decorator on the three
  pipeline routes, and the `enforce_default_limit` dependency for everything
  else — that each independently no-op unless `api.auth == "supabase"` (the
  decorator via `exempt_when=_local_tier_exempt`, `ratelimit.py`). Both must
  agree for the local tier to keep zero rate ceiling, its own binding
  constraint. Supabase Auth's configurable built-in limits cover the auth
  endpoints. Considered, deferred: Cloudflare free tier in front of the
  domain.

### Known follow-ups

Not fixed here, deliberately: `public.access_requests` has RLS enabled with
zero policies — fail-closed for the `authenticated` role (deny-all),
reachable only via the service role. Safe by default; revisit if the table is
ever read from a user-scoped client.

## D. Frontend: landing, app, onboarding

- **Landing `/`:** one headline naming the outcome (grounded,
  self-correcting knowledge graphs from research), subheadline naming the
  audience (developers/agent builders), real product screenshot, **single
  primary CTA** (get early access → Tally waitlist), open-core framing
  (local tier free forever · hosted beta by invite), GitHub link, `/demo`
  link. OG/meta tags static in `index.html` (`og:image` 1200×630 asset);
  prerendering is post-launch.
- **`/app`:** session guard — no session → sign-in/up; session without
  `beta_members` row → waitlist screen (the SPA reads its own row via
  supabase-js for UX; the engine's 403 is the enforcement); else the
  dashboard scoped to the user's org. **`/app` never silently falls back to mock data**: API failure
  renders an explicit error state. The mock fallback moves to `/demo` (and
  `VITE_USE_MOCK=1` dev).
- **API client:** attaches the supabase-js access token as a Bearer header
  when a session exists; 401 → sign-in; 403 → waitlist screen.
- **Legal:** `/terms` + `/privacy`, generator-produced (TermsFeed/GetTerms),
  naming Supabase, Vercel, Fly, Resend, PostHog as subprocessors.
- **First-run:** a new org's empty dashboard names the value and offers one
  guided action — "run your first explore" with a pre-filled topic, streamed
  via the existing SSE route — targeting < 5 min to a populated graph.
- **State rule (QA lesson):** KB/org switch while inside any view resets
  that view's state; tested for the new surfaces, not just entry paths.

## E. Hardening & ops

Free tiers throughout (use-over-build):

- **Error tracking:** Sentry — Fly extension for the backend (free year of
  Team), `@sentry/react` on the SPA (free tier), DSNs via env.
- **Uptime + status page:** UptimeRobot free tier monitoring `/` and a
  cheap engine health endpoint.
- **Analytics:** PostHog free tier, **cookieless** (`persistence:
  'memory'`) — no cookie banner needed at launch, privacy-policy disclosure
  suffices. Exactly two launch events: `signed_up`,
  `first_explore_completed`.
- **Backups:** confirm the Supabase plan tier (not exposed via the
  management API). Free plan → either upgrade to Pro (daily backups) or a
  scheduled `pg_dump` via GitHub Actions during the invite-only phase; test
  one restore either way.
- **Secrets audit:** nothing in git in either repo; Fly secrets / Vercel
  env / `supabase secrets set`; rotate anything found committed.
- **Loud-failure fixes ship first** (pulled forward from the Canvas draft —
  they sit on the hosted path): (1) a rejected search provider (e.g. Tavily
  quota / HTTP 432) records the exploration `failed` and emits a typed SSE
  error frame — never `count=0` marked `completed`; (2) synopsis rebuild
  failure is surfaced in results, never swallowed.

## F. Data flow

Signup (Turnstile) → Supabase Auth → `handle_new_user` provisions org +
membership → SPA session → `beta_members` check → `/api` calls with Bearer
JWT → verifier → `TenantContext(org)` → org-scoped Store reads/writes with
RLS as the second wall. Explore streams over the existing SSE route with the
same token.

## G. Error handling

- 401 invalid/absent token; 403 authenticated but not in `beta_members`;
  429 with `Retry-After` on rate limit; typed SSE error frames on pipeline
  failures.
- All of the above captured by Sentry on both sides.
- `/app` renders explicit error and empty states — no silent mock, no
  silent empty.

## H. Testing

- **Local suite stays hermetic** (auth defaults `none`; no cloud
  dependency) — untouched, per invariant.
- **Auth dependency units:** fake verifier; expired/garbage/absent token →
  401; valid token without beta grant → 403.
- **Two-user isolation test** (acceptance criterion): user B cannot read or
  write user A's projects, KBs, findings, or graph — against
  `fake_supabase.py`, plus an opt-in production smoke variant.
- **RLS audit script:** asserts org-scoped SELECT and `WITH CHECK` write
  policies exist on every tenant table (catches the `kg_schemas`-class gap
  mechanically); runs in CI against the migrations' policy set.
- **Frontend:** guard redirect paths, waitlist gate, empty state render,
  no-mock-in-`/app` failure state, KB/org-switch-in-view resets.
- **Manual QA:** the stranger round-trip (landing → invite → sign-up →
  verified email → first-run → populated graph) executed before the
  sign-up link goes public.

## I. Build order

1. Loud-failure fixes; backend auth dependency + org-scoped resolution;
   `beta_members` migration; RLS audit script.
2. Frontend auth screens, `/app` guard + waitlist gate, API client Bearer
   wiring, org scoping end to end.
3. Landing page, legal pages, `/demo` split, OG tags.
4. Hardening wiring: Resend SMTP, Turnstile, Sentry ×2, UptimeRobot,
   PostHog (cookieless), backups verification, secrets audit.
5. Onboarding empty state + guided first explore; invite the first users.

Out of scope (vision non-goals): billing/Stripe, multi-member orgs,
Canvas v1, br8n.

---

## Appendix A — launch-readiness research (2026-07-19)

Persisted to the `delapan/master` KB as 24 findings (synopsis rebuilt).
Tags: MUST-HAVE (blocks public launch) / SHOULD-HAVE (launch week) /
POST-LAUNCH.

### Landing page
One outcome-naming headline + audience subheadline + product screenshot
(MUST); single primary CTA above the fold — single-CTA pages convert ~13.5%
vs ~10.5% for 5+ (MUST); named social proof (SHOULD); persona-framed pricing
with the free tier surfaced early (SHOULD); interactive demo (POST).
Sources: userpilot.com/blog/saas-landing-pages, studiomaydit.com
saas-landing-page-best-practices-2026, markepear.dev/examples/pricing.

### Auth & sign-in
Email confirmation + working reset (MUST); custom SMTP — Supabase's built-in
provider caps at 2 emails/hour (MUST); OTP expiry ≤ 1h + CAPTCHA (MUST); one
OAuth provider, GitHub for dev audiences (SHOULD); magic links/passkeys
(POST). Sources: supabase.com/docs/guides/auth/rate-limits,
…/deployment/going-into-prod, …/auth/auth-smtp.

### Accounts & multi-tenancy
RLS on every tenant table, org-scoped, indexed, `WITH CHECK` on writes
(MUST); membership table + security-definer helper (MUST); RLS tests — RLS
failures return silent empty rows, not errors (SHOULD); per-user prefixed
hashed API keys with rotation (SHOULD); fine-grained permissions (POST).
Sources: makerkit.dev supabase-rls-best-practices, github.com/orgs/supabase
discussion 1615, zuplo.com api-key-authentication.

### Billing
Launching unbilled/invite-gated is the validated norm — demand signal before
charging (MUST-decision); Stripe Checkout + 3 webhooks, Customer Portal,
metering all POST-LAUNCH (~1–2 weeks build when justified). Sources:
docs.stripe.com saas-subscriptions, indiehackers.com solo-launch threads.

### Legal & compliance
ToS + privacy policy naming data + subprocessors, required the moment
sign-up exists (MUST); subprocessor DPAs on file for EU scope (MUST);
cookie banner only if non-essential cookies ship — cookieless analytics
avoids it (conditional); generator-produced over hand-drafted (SHOULD).
Sources: termsfeed.com, gdpr.eu/cookies, supabase.com DPA.

### Production hardening
Rate limiting keyed user/org/IP on every public endpoint (MUST); Sentry on
backend + frontend — Fly ships a one-command extension with a free year
(MUST); backups verified with one tested restore (MUST); secrets out of git
(MUST); uptime monitor + public status page (SHOULD); load testing (POST).
Sources: fly.io/docs/apps/going-to-production, fly.io/blog/sentry-partnership,
supabase.com/docs/guides/platform/backups.

### Onboarding
Value-naming first-run empty state (MUST); one quickstart path to a
populated first result < 5 min (MUST); in-product help layer (SHOULD);
seeded demo option (SHOULD); tours (POST). Sources: evilmartians.com
dev-tools onboarding, 72technologies.com empty-states.

### Launch-day & post-launch
Minimal analytics — signup + one activation event (MUST); OG tags + meta on
public pages (MUST); Product Hunt/HN prep if using that channel (SHOULD);
activation/retention benchmarks ~30–40% "aha" in 7 days, D1 ~50%+, D7 ~25%+
(POST). Sources: posthog.com activation-metrics, getlaunchlist.com
producthunt checklist.

### Merged gate list (as applied in §I build order)
Legal pages → Supabase Auth production settings (SMTP, confirmation,
CAPTCHA, OTP) → RLS audit → auth-gate the dashboard → GitHub OAuth →
rate limiting → Sentry → backups → secrets → uptime/status → landing +
OG + analytics → first-run empty state → billing deferred.
(Per-user API keys for the MCP/API surface: SHOULD-HAVE, post-launch here —
claude.ai OAuth covers the MCP surface at launch.)
