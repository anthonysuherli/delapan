# delapan-fe as prod — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sigma.js `delapan-fe` dashboard the served app at `delapan.ai` — gated behind Supabase login, backed by a newly-deployed open-core `/api` engine, as an invite-only beta.

**Architecture:** Three workstreams. (A) Add a Supabase session gate around the `delapan-fe` SPA and attach the JWT to every engine call. (B) Deploy the open-core `delapan.api.main` FastAPI app as a new Fly service (`delapan-api`) with `api.auth=supabase` against the shared cloud Supabase. (C) Point a Vercel project at `delapan-fe` and move the `delapan.ai` domain onto it. Staged mock→real→domain, each stage reversible.

**Vision goals served:** *"Hosted public tier with account isolation"* (account-gated dashboard + authenticated engine API + invite-gated beta); realizes the Non-Goal *"evolve the existing sigma.js control panel, not a greenfield dashboard."*

**Tech Stack:** React 18 + Vite 6 + TypeScript strict + Zustand + sigma.js (frontend); FastAPI + uvicorn (backend); Supabase Auth (ES256/JWKS JWTs); Fly.io; Vercel.

## Global Constraints

- **Two repos.** Frontend tasks (A, C) run in `delapan-ai/frontend` (repo `delapan-fe`, branch off `main`). Backend tasks (B) run in `~/projects/delapan` on branch `feat/delapan-fe-as-prod` (already created). Create a frontend branch `feat/auth-gate-prod` before Task A1.
- **Frontend build gate:** `npm run build` (`tsc --noEmit` strict: `noUnusedLocals`/`noUnusedParameters` — unused imports fail). Run before claiming any frontend change compiles.
- **Vitest is node-env (no DOM).** Test pure logic, not React render. Tests live next to code as `*.test.ts`.
- **Mock parity:** every endpoint in `src/api/client.ts` has a live + mock impl; auth headers touch only the live path, so `src/api/mock.ts` is unchanged — do not break it.
- **No hardcoded config** (backend): defaults < `config.yaml` < env (`DLP_<SECTION>__<FIELD>`, nested delimiter `__`; `CORS_ORIGINS` is a top-level `Settings` alias).
- **Shared cloud Supabase project:** `gunqbyddzuwzpncfigro`. RLS is org-scoped and load-bearing — do not weaken it.
- **Outward-facing gates:** the Fly deploy (B2), the domain move (C3), and any `vercel --prod` are hard-to-reverse; pause for explicit human confirmation before running them.

---

## Workstream A — Frontend auth gate (`delapan-fe`)

### Task A1: Attach the Supabase JWT to every engine call

**Files:**
- Modify: `src/api/client.ts` (add `authHeaders()`; inject into `http()`, `liveExplore`, and the canvas SSE fetch if present)
- Test: `src/api/authHeaders.test.ts`

**Interfaces:**
- Produces: `export async function authHeaders(): Promise<Record<string, string>>` — returns `{ Authorization: "Bearer <token>" }` when a Supabase session exists, `{}` otherwise (never throws).
- Consumes: `getSupabaseClient` from `../tracking/supabaseClient`.

- [ ] **Step 1: Write the failing test**

```ts
// src/api/authHeaders.test.ts
import { describe, expect, it, vi, beforeEach } from "vitest";

const getSession = vi.fn();
vi.mock("../tracking/supabaseClient", () => ({
  getSupabaseClient: () => ({ auth: { getSession } }),
}));

import { authHeaders } from "./client";

describe("authHeaders", () => {
  beforeEach(() => getSession.mockReset());

  it("returns a Bearer header when a session exists", async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: "tok123" } } });
    expect(await authHeaders()).toEqual({ Authorization: "Bearer tok123" });
  });

  it("returns an empty object when there is no session", async () => {
    getSession.mockResolvedValue({ data: { session: null } });
    expect(await authHeaders()).toEqual({});
  });

  it("never throws when the client is unconfigured", async () => {
    getSession.mockRejectedValue(new Error("not configured"));
    expect(await authHeaders()).toEqual({});
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- authHeaders`
Expected: FAIL — `authHeaders` is not exported from `./client`.

- [ ] **Step 3: Add `authHeaders()` and inject it**

In `src/api/client.ts`, add the import near the top:

```ts
import { getSupabaseClient } from "../tracking/supabaseClient";
```

Add after the `BASE` constant:

```ts
/** Bearer header for the current Supabase session, or {} when signed out.
 *  Read fresh per request — supabase-js auto-refreshes the access token. */
export async function authHeaders(): Promise<Record<string, string>> {
  try {
    const { data } = await getSupabaseClient().auth.getSession();
    const token = data.session?.access_token;
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}
```

Update `http()` to await auth headers (callers never pass `init.headers`, so this preserves behavior):

```ts
async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.text();
      if (body) detail = body.slice(0, 300);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
```

Update the SSE fetch in `liveExplore` (add auth to its headers):

```ts
  const res = await fetch(`${BASE}${kbPath(project, kb)}/explore`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(body),
  });
```

(If a canvas SSE fetch with the same `fetch(...)` shape exists in this file, apply the identical `...(await authHeaders())` change there too.)

- [ ] **Step 4: Run tests + typecheck**

Run: `npm run test -- authHeaders && npm run build`
Expected: tests PASS; build completes with no type errors.

- [ ] **Step 5: Commit**

```bash
git add src/api/client.ts src/api/authHeaders.test.ts
git commit -m "feat(api): attach Supabase JWT to every engine call"
```

### Task A2: Sign out on 401 so the gate re-challenges

**Files:**
- Modify: `src/api/client.ts` (`http()` 401 branch)
- Test: `src/api/authHeaders.test.ts` (extend)

**Interfaces:**
- Consumes: `getSupabaseClient().auth.signOut()`; `ApiError` from `./types`.

- [ ] **Step 1: Write the failing test** (append to `authHeaders.test.ts`)

```ts
import { on401SignOut } from "./client";

describe("on401SignOut", () => {
  it("calls signOut only for 401", async () => {
    const signOut = vi.fn().mockResolvedValue({ error: null });
    // @ts-expect-error partial client for test
    on401SignOut(401, { auth: { signOut } });
    expect(signOut).toHaveBeenCalledOnce();
    signOut.mockReset();
    // @ts-expect-error partial client for test
    on401SignOut(500, { auth: { signOut } });
    expect(signOut).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- authHeaders`
Expected: FAIL — `on401SignOut` is not exported.

- [ ] **Step 3: Implement**

Add to `src/api/client.ts`:

```ts
import type { SupabaseClient } from "@supabase/supabase-js";

/** A 401 means the JWT is missing/expired/invalid — drop the session so the
 *  AuthGate re-renders the login screen. Fire-and-forget; never rethrow here. */
export function on401SignOut(status: number, client: Pick<SupabaseClient, "auth">): void {
  if (status === 401) void client.auth.signOut();
}
```

In `http()`, inside the `if (!res.ok)` block, before `throw new ApiError(...)`:

```ts
    on401SignOut(res.status, getSupabaseClient());
```

- [ ] **Step 4: Run tests + build**

Run: `npm run test -- authHeaders && npm run build`
Expected: PASS; build clean.

- [ ] **Step 5: Commit**

```bash
git add src/api/client.ts src/api/authHeaders.test.ts
git commit -m "feat(api): sign out on 401 so the auth gate re-challenges"
```

### Task A3: Wrap the app in an AuthGate

**Files:**
- Create: `src/auth/AuthGate.tsx`
- Modify: `src/main.tsx` (wrap `<App />`)

**Interfaces:**
- Produces: `export function AuthGate({ children }: { children: React.ReactNode }): JSX.Element` — renders `children` only when a Supabase session exists; otherwise the login screen; a spinner while the session is resolving. Mirrors `src/tracking/TrackingApp.tsx`'s session pattern.

- [ ] **Step 1: Create `src/auth/AuthGate.tsx`**

```tsx
import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { SignInForm } from "../tracking/SignInForm";
import { getSupabaseClient } from "../tracking/supabaseClient";

/** Session gate around the whole dashboard. No session → login; undefined →
 *  "checking session"; session → children. Mirrors TrackingApp's auth pattern. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null | undefined>(undefined);

  let supabase: ReturnType<typeof getSupabaseClient> | null = null;
  try {
    supabase = getSupabaseClient();
  } catch (err) {
    return (
      <main className="tracking-state">
        <p className="tracking-error">
          {err instanceof Error ? err.message : "Auth is not configured."}
        </p>
      </main>
    );
  }

  useEffect(() => {
    let active = true;
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_e, next) => {
      if (active) setSession(next);
    });
    void supabase.auth.getSession().then(({ data }) => {
      if (active) setSession(data.session);
    });
    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, [supabase]);

  if (session === undefined) {
    return (
      <main className="tracking-state">
        <span className="spin" /> checking session…
      </main>
    );
  }
  if (!session) {
    return (
      <SignInForm
        supabase={supabase}
        title="delapan"
        subtitle="Sign in to your delapan account."
      />
    );
  }
  return <>{children}</>;
}
```

- [ ] **Step 2: Wrap `<App />` in `src/main.tsx`**

Change the render line so the main app is gated (leave `/tracking` and `/duet` as-is — they gate themselves):

```tsx
import { AuthGate } from "./auth/AuthGate";
// ...
root.render(
  path === "/tracking" ? (
    <TrackingApp />
  ) : path === "/duet" ? (
    <DuetApp />
  ) : (
    <AuthGate>
      <App />
    </AuthGate>
  ),
);
```

- [ ] **Step 3: Typecheck the build**

Run: `npm run build`
Expected: build completes; no unused-import/type errors.

- [ ] **Step 4: Manual smoke (dev)**

Run: `npm run dev`, open `http://localhost:5173/` with `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` set.
Expected: login screen when signed out; after `signInWithPassword` (a seeded beta user), the sigma dashboard renders.

- [ ] **Step 5: Commit**

```bash
git add src/auth/AuthGate.tsx src/main.tsx
git commit -m "feat(auth): gate the dashboard behind a Supabase session"
```

### Task A4: Prod env sample + SPA rewrite

**Files:**
- Modify: `.env.example`, `vercel.json`

- [ ] **Step 1: Extend `.env.example`**

Add under the existing keys:

```bash
# Prod (delapan.ai): point at the deployed open-core /api backend and leave mock off.
# VITE_API_BASE=https://delapan-api.fly.dev
# VITE_USE_MOCK=
```

- [ ] **Step 2: Add an SPA catch-all to `vercel.json`**

```json
{
  "rewrites": [
    { "source": "/tracking", "destination": "/index.html" },
    { "source": "/duet", "destination": "/index.html" },
    { "source": "/(.*)", "destination": "/index.html" }
  ]
}
```

- [ ] **Step 3: Build to confirm nothing broke**

Run: `npm run build`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add .env.example vercel.json
git commit -m "chore(deploy): prod env sample + SPA catch-all rewrite"
```

---

## Workstream B — Deploy the open-core `/api` app (`~/projects/delapan`)

### Task B1: Fly config for a `delapan-api` REST service

**Files:**
- Create: `fly.api.toml`

**Interfaces:**
- The existing `Dockerfile` builds the whole engine; a Fly `[processes]` entry overrides its `CMD` to run the REST app instead of the MCP server. Reuses the same image, region, and env conventions as `fly.toml`.

- [ ] **Step 1: Create `fly.api.toml`**

```toml
# Open-core /api REST surface (delapan.api.main) — backs the delapan-fe dashboard.
app = 'delapan-api'
primary_region = 'sjc'

[build]

[env]
  DELAPAN_BACKEND = 'cloud'
  DLP_API__AUTH = 'supabase'
  PORT = '8000'
  CORS_ORIGINS = 'https://delapan.ai'

[processes]
  app = 'uvicorn delapan.api.main:app --host 0.0.0.0 --port 8000'

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = 'off'
  auto_start_machines = true
  min_machines_running = 1

[[vm]]
  size = 'shared-cpu-1x'
  memory = '512mb'
```

- [ ] **Step 2: Commit**

```bash
git add fly.api.toml
git commit -m "feat(deploy): fly config for the delapan-api REST service"
```

### Task B2: Provision + deploy the API app  ⚠️ outward-facing — confirm first

**Files:** none (infra)

- [ ] **Step 1: Create the Fly app**

Run: `fly apps create delapan-api`
Expected: `New app created: delapan-api` (or "already exists" — then continue).

- [ ] **Step 2: Set secrets** (same Supabase project + LLM/embedding keys the MCP app uses; copy the values from the existing `delapan-cloud-mcp` secrets or `~/projects/delapan/.env`)

Run (fill each value):
```bash
fly secrets set --app delapan-api \
  SUPABASE_URL="https://gunqbyddzuwzpncfigro.supabase.co" \
  SUPABASE_SERVICE_ROLE_KEY="…" \
  SUPABASE_ANON_KEY="…" \
  AI_GATEWAY_API_KEY="…"
```
Expected: `Secrets are staged for the first deployment`.
Note: confirm the exact secret names the engine reads by checking `~/projects/delapan/.env` / `delapan/core/config.py` aliases before running; do not invent names.

- [ ] **Step 3: Deploy**

Run: `fly deploy --app delapan-api --config fly.api.toml`
Expected: build + release succeed; `1 machine(s) running`.

- [ ] **Step 4: Verify health (unauthenticated)**

Run: `curl -sS -o /dev/null -w '%{http_code}\n' https://delapan-api.fly.dev/health`
Expected: `200`.

- [ ] **Step 5: Verify auth is enforced (no token → 401)**

Run: `curl -sS -o /dev/null -w '%{http_code}\n' https://delapan-api.fly.dev/api/projects`
Expected: `401` (auth = supabase; no bearer token).

- [ ] **Step 6: Verify a real token works** (mint a beta-user JWT, then:)

Run: `curl -sS -H "Authorization: Bearer $JWT" https://delapan-api.fly.dev/api/projects | head -c 200`
Expected: a JSON projects payload (HTTP 200), scoped to that user's org.

### Task B3: RLS audit before any public traffic  ⚠️ load-bearing invariant

**Files:** none (verification)

- [ ] **Step 1: Run the audit against the shared cloud project**

Run: `python scripts/rls_audit.py`
Expected: no findings for the KB/finding/graph tables the `/api` app reads (projects, kbs, findings, kg nodes/edges). Any gap → fix the migration/policy and re-run before proceeding to Workstream C.

- [ ] **Step 2: Cross-org isolation smoke**

With two beta users' JWTs, `GET /api/projects` as user B must not return user A's projects.
Expected: disjoint project sets. Record the result in the PR description.

---

## Workstream C — Vercel project + domain cutover

### Task C1: Deploy delapan-fe to a preview URL on mock

**Files:** none (infra; run from `delapan-ai/frontend`)

- [ ] **Step 1: Link a Vercel project**

Run: `vercel link` (create a new project, e.g. `delapan-fe`; Vite preset auto-detected).
Expected: `.vercel/project.json` written.

- [ ] **Step 2: Set preview env vars (mock, no data exposure)**

Run:
```bash
vercel env add VITE_SUPABASE_URL preview          # https://gunqbyddzuwzpncfigro.supabase.co
vercel env add VITE_SUPABASE_ANON_KEY preview     # anon key
vercel env add VITE_USE_MOCK preview              # 1
```

- [ ] **Step 3: Deploy preview**

Run: `vercel --yes`
Expected: a `*.vercel.app` preview URL.

- [ ] **Step 4: Verify the gate + build**

Open the preview URL. Expected: login screen when signed out; after sign-in, the dashboard renders **mock** data (proves the pipeline with zero real-data exposure).

### Task C2: Point preview at the real backend

**Files:** none (infra)

- [ ] **Step 1: Swap env to live**

Run:
```bash
vercel env rm VITE_USE_MOCK preview               # remove mock
vercel env add VITE_API_BASE preview              # https://delapan-api.fly.dev
```

- [ ] **Step 2: Redeploy + verify real data behind auth**

Run: `vercel --yes`
Open the preview, sign in as a beta user.
Expected: the dashboard loads **real** KB data; browser devtools show each `/api/...` request carrying `Authorization: Bearer …`; signing out returns to the login screen.

### Task C3: Move `delapan.ai` onto delapan-fe  ⚠️ production domain — confirm first

**Files:** none (infra)

- [ ] **Step 1: Promote prod env** (mirror the preview live env into Production scope: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_BASE`; `VITE_USE_MOCK` unset). Deploy prod: `vercel --prod --yes`.

- [ ] **Step 2: Move the domain**

In the Vercel dashboard (or CLI): remove `delapan.ai` from the Next.js project (`delapan`) and add it to the `delapan-fe` project. Expected: `delapan.ai` resolves to the new deployment; DNS already points at Vercel so no registrar change is needed.

- [ ] **Step 3: Verify prod**

Run: `curl -sSL -o /dev/null -w '%{http_code}\n' https://delapan.ai`
Open `https://delapan.ai`: login screen when signed out; dashboard with real data after sign-in.

- [ ] **Step 4: Record rollback**

Rollback = re-add `delapan.ai` to the `delapan` (Next.js) project. Document this one-line rollback in the PR.

---

## Self-Review

- **Spec coverage:** A1–A3 = frontend auth gate + JWT (spec §A); A4 + C = Vercel/domain (spec §C); B = backend `/api` deploy (spec §B); B3 = RLS invariant (spec risks); out-of-scope landing/legal explicitly deferred. All spec sections mapped.
- **Placeholders:** none — every code step shows full content; infra steps give exact commands + expected output. Secret *names* in B2 are flagged for verification against `.env` rather than invented.
- **Type consistency:** `authHeaders()`/`on401SignOut()` signatures match between definition and use; `AuthGate` mirrors the verified `TrackingApp` session pattern; `SignInForm` reused with its existing `title`/`subtitle` props.

## Execution notes / open items folded in
- delapan-fe routing is manual path-switching in `main.tsx` (no router) — the SPA catch-all in A4 covers deep links; `/tracking` + `/duet` keep self-gating.
- No Vercel project is linked yet (`.vercel/` absent) — C1 creates one.
- Confirm the exact Supabase/LLM secret names in `~/projects/delapan/.env` before B2.
