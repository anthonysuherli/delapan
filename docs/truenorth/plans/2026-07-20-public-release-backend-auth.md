# Public Release Backend Auth (Plan 1 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the engine's `/api` surface a config-gated Supabase-auth tier (JWT verification, `beta_members` invite gate, org-scoped tenancy), rate limiting, an RLS audit, and cloud serving beside the MCP server — while the local tier stays byte-identical and auth-less.

**Architecture:** A new `delapan/api/auth.py` provides FastAPI dependencies that fork on `config.api.auth`: `"none"` (default) delegates to the existing `resolve_kb_or_404` / `resolve_store`; `"supabase"` verifies the bearer JWT locally (pyjwt + `SUPABASE_JWT_SECRET` — no per-request GoTrue round-trip), enforces the `beta_members` allowlist via the service client, and resolves tenancy through the proven `resolve_tenant_for_token`. Routes swap their inline resolution calls for these dependencies. The cloud MCP server's Starlette app additionally mounts the REST app so one Fly deploy serves both.

**Vision goals served:** *Hosted public tier with account isolation* (End Goal added 2026-07-19); *Two tiers at protocol parity* (one `/api` contract, auth by config).

**Tech Stack:** FastAPI, pyjwt (already in `[cloud]`), supabase-py service client, slowapi (new, `[cloud]`), asyncpg (already in `[cloud]`), pytest + TestClient.

## Global Constraints

- **Coordination:** another session recently worked this checkout (commits `b2e9b20`, `0cff1ac`; loud-failure chain + key-gate fix). Before Task 1: `git status` must be clean and `git log --oneline -5` reviewed; rebase/absorb anything pending. Do NOT modify `tests/test_explore_loud_failure.py`, the `missing_pipeline_keys` block of `delapan/api/deps.py`, or canvas internals beyond the two `Depends` swaps listed in Task 5.
- **Parity invariant:** with `api.auth: "none"` every route's behavior is byte-identical to today. The full existing suite must stay green after every task.
- **Hermetic tests:** no network, no production cloud. Cloud-path tests use forged JWTs with a test secret, fake service clients, and `tests/fake_supabase.py`.
- **Config, never hardcoded:** new knobs live in `ApiConfig` (`config.yaml` `api:` section, env override `DLP_API__<FIELD>`). Secrets stay in `Settings`/`.env`.
- House style: `from __future__ import annotations`, type hints, terse module docstrings with ASCII flow diagram, ruff line-length 100.
- Run tests with `uv run pytest` from the repo root (`~/projects/delapan`).

---

### Task 1: `ApiConfig` section

**Files:**
- Modify: `delapan/core/config.py` (section classes end ~line 430; `AppConfig` at 434-445)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `get_config().api` → `ApiConfig(auth: str, rate_limit_default: str, rate_limit_pipeline: str)`. Consumed by Tasks 3, 5, 6, 9.

- [ ] **Step 1: Write the failing test** (append to `tests/test_config.py`, mirroring its existing env-override tests)

```python
def test_api_config_defaults_and_env_override(monkeypatch):
    from delapan.core.config import get_config

    get_config.cache_clear()
    assert get_config().api.auth == "none"
    assert get_config().api.rate_limit_default == "120/minute"
    assert get_config().api.rate_limit_pipeline == "12/hour"

    monkeypatch.setenv("DLP_API__AUTH", "supabase")
    get_config.cache_clear()
    assert get_config().api.auth == "supabase"
    get_config.cache_clear()
```

(If `get_config` in this repo isn't `lru_cache`'d, drop the `cache_clear()` calls and follow whatever reset idiom the existing tests in `tests/test_config.py` use — copy their pattern exactly.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_api_config_defaults_and_env_override -v`
Expected: FAIL — `AttributeError: 'AppConfig' object has no attribute 'api'`

- [ ] **Step 3: Implement** — in `delapan/core/config.py`, after the last section class (before `class AppConfig`):

```python
class ApiConfig(BaseModel):
    """The HTTP /api surface — auth mode and rate limits."""

    auth: str = "none"  # "none" (local, auth-less) | "supabase" (bearer JWT + beta gate)
    rate_limit_default: str = "120/minute"  # per user-or-IP, all routes
    rate_limit_pipeline: str = "12/hour"  # explore/canvas POSTs (LLM + search spend)
```

and inside `AppConfig`, alongside the existing fields:

```python
    api: ApiConfig = Field(default_factory=ApiConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add delapan/core/config.py tests/test_config.py
git commit -m "feat(config): api section — auth mode + rate-limit knobs"
```

---

### Task 2: Local JWT bearer verification

**Files:**
- Create: `delapan/api/auth.py`
- Test: Create `tests/test_api_auth.py`

**Interfaces:**
- Consumes: `get_settings().supabase_jwt_secret` (exists, `config.py:67`).
- Produces: `verify_bearer(authorization: str | None) -> str` (returns Supabase user id; raises `fastapi.HTTPException(401)` / `HTTPException(500)`). Consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

```python
"""Auth-layer tests: forged HS256 JWTs against a test secret — hermetic."""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

SECRET = "test-jwt-secret"


def _token(sub: str = "user-a", *, aud: str = "authenticated", exp_delta: int = 3600,
           secret: str = SECRET) -> str:
    return jwt.encode(
        {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta}, secret, algorithm="HS256"
    )


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_verify_bearer_valid_returns_sub():
    from delapan.api.auth import verify_bearer

    assert verify_bearer(f"Bearer {_token('user-9')}") == "user-9"


@pytest.mark.parametrize(
    "header",
    [None, "", "Token abc", f"Bearer {jwt.encode({'sub': 'x'}, 'wrong', algorithm='HS256')}"],
)
def test_verify_bearer_missing_or_invalid_401(header):
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(header)
    assert exc.value.status_code == 401


def test_verify_bearer_expired_401():
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {_token(exp_delta=-10)}")
    assert exc.value.status_code == 401


def test_verify_bearer_wrong_audience_401():
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {_token(aud='not-authenticated')}")
    assert exc.value.status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.api.auth'`

- [ ] **Step 3: Implement `delapan/api/auth.py`**

```python
"""Bearer auth + beta gating for the public /api surface.

    Authorization: Bearer <supabase JWT>
        │ api.auth == "none"     ──► resolve_kb_or_404 / resolve_store (unchanged)
        └ api.auth == "supabase" ──► verify_bearer (pyjwt HS256, aud="authenticated")
                                       ──► require_beta (beta_members via service client)
                                       ──► resolve_*_for_token ──► (ctx, org-scoped store)

Tokens are verified locally against SUPABASE_JWT_SECRET — no GoTrue round-trip
per request. The cloud MCP server keeps its own SupabaseTokenVerifier; same
tokens, different transport.
"""

from __future__ import annotations

from fastapi import HTTPException

from delapan.core.config import get_settings


def verify_bearer(authorization: str | None) -> str:
    """Validate an `Authorization: Bearer <jwt>` header; return the Supabase user id."""
    import jwt  # pyjwt — [cloud] extra

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    secret = get_settings().supabase_jwt_secret
    if not secret:
        raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET not configured")
    try:
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc
    return claims["sub"]
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/test_api_auth.py -v` — Expected: PASS ×7.

- [ ] **Step 5: Commit**

```bash
git add delapan/api/auth.py tests/test_api_auth.py
git commit -m "feat(api): local JWT bearer verification (pyjwt, SUPABASE_JWT_SECRET)"
```

---

### Task 3: Beta gate + tenancy dependencies

**Files:**
- Modify: `delapan/api/auth.py` (append)
- Test: `tests/test_api_auth.py` (append)

**Interfaces:**
- Consumes: `verify_bearer` (Task 2); `get_config().api.auth` (Task 1); `resolve_kb_or_404` (`api/deps.py:22`, returns `tuple[TenantContext, Store]`); `resolve_tenant_for_token(user_id, access_token, project, kb, *, create) -> TenantContext` and `resolve_store` / `resolve_store_for_token` (`mcp/tenancy.py`); `service_client()` (`core/clients/supabase.py`); `get_store(token, org_id=...)` (`delapan/store`).
- Produces (consumed by Task 5):
  - `request_tenancy(project: str, kb: str, request: Request) -> tuple[TenantContext, Store]`
  - `request_tenancy_creating(...)` — same signature; cloud path resolves with `create=True` (explore only — mirrors the MCP explore semantics; the dashboard's first-run guided explore needs it; spec §D). Local path never creates.
  - `require_beta(user_id: str) -> None` (403 without a `beta_members` row)
  - `request_store(request: Request) -> Store`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_api_auth.py`)

```python
class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_):  # chainable PostgREST fake
        return self

    def eq(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        import types

        return types.SimpleNamespace(data=self._rows)


class _FakeService:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "beta_members"
        return _FakeTable(self._rows)


def test_require_beta_member_passes(monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    from delapan.api.auth import require_beta

    require_beta("u1")  # no raise


def test_require_beta_non_member_403(monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([]))
    from delapan.api.auth import require_beta

    with pytest.raises(HTTPException) as exc:
        require_beta("u2")
    assert exc.value.status_code == 403


def test_request_tenancy_auth_none_delegates(monkeypatch):
    """With api.auth=none the dependency is exactly resolve_kb_or_404 — parity."""
    import delapan.api.auth as auth_mod

    sentinel = ("ctx", "store")
    monkeypatch.setattr(auth_mod, "resolve_kb_or_404", lambda p, k: sentinel)
    monkeypatch.delenv("DLP_API__AUTH", raising=False)
    from delapan.core.config import get_config

    get_config.cache_clear()
    from delapan.api.auth import request_tenancy

    class _Req:  # request is unused on the auth-none path
        headers: dict = {}

    assert request_tenancy("p", "k", _Req()) == sentinel
    get_config.cache_clear()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api_auth.py -v` — Expected: new tests FAIL (`ImportError: cannot import name 'require_beta'`).

- [ ] **Step 3: Implement** (append to `delapan/api/auth.py`)

```python
from fastapi import Request  # add beside the existing fastapi import

from delapan.api.deps import resolve_kb_or_404
from delapan.core.agent.state import TenantContext
from delapan.core.config import get_config
from delapan.store import Store, get_store


def _service_client():
    """Service-role client, lazily imported — module-level seam for test fakes."""
    from delapan.core.clients.supabase import service_client  # lazy: cloud-only

    return service_client()


def require_beta(user_id: str) -> None:
    """403 unless the user holds a beta_members row. Service client + explicit
    filter, matching tenancy.py's documented convention."""
    rows = (
        _service_client()
        .table("beta_members")
        .select("user_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not rows.data:
        raise HTTPException(status_code=403, detail="beta access required")


def _authed_tenancy(project: str, kb: str, request: Request, *, create: bool):
    from delapan.mcp.tenancy import resolve_tenant_for_token

    authorization = request.headers.get("authorization")
    user_id = verify_bearer(authorization)
    require_beta(user_id)
    token = authorization.removeprefix("Bearer ")
    try:
        ctx = resolve_tenant_for_token(user_id, token, project, kb, create=create)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ctx, get_store(ctx.access_token, org_id=ctx.org_id)


def request_tenancy(project: str, kb: str, request: Request) -> tuple[TenantContext, Store]:
    """KB-scoped dependency: auth-none delegates untouched; supabase verifies + gates."""
    if get_config().api.auth != "supabase":
        return resolve_kb_or_404(project, kb)
    return _authed_tenancy(project, kb, request, create=False)


def request_tenancy_creating(project: str, kb: str, request: Request) -> tuple[TenantContext, Store]:
    """Explore-only variant: the cloud path may create the project/KB on demand
    (mirrors MCP explore; the dashboard's first-run explore needs it). Local
    HTTP stays non-creating."""
    if get_config().api.auth != "supabase":
        return resolve_kb_or_404(project, kb)
    return _authed_tenancy(project, kb, request, create=True)


def request_store(request: Request) -> Store:
    """Org-scoped store with no project/kb binding (projects/discovery routes)."""
    if get_config().api.auth != "supabase":
        from delapan.mcp.tenancy import resolve_store

        return resolve_store()
    from delapan.mcp.tenancy import resolve_store_for_token

    authorization = request.headers.get("authorization")
    user_id = verify_bearer(authorization)
    require_beta(user_id)
    return resolve_store_for_token(user_id, authorization.removeprefix("Bearer "))
```

Update the module docstring's flow diagram if it drifted. Keep ruff happy (line 100).

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/test_api_auth.py -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add delapan/api/auth.py tests/test_api_auth.py
git commit -m "feat(api): beta gate + config-forked tenancy dependencies"
```

---

### Task 4: `beta_members` migration

**Files:**
- Create: `migrations/2026-07-20-beta-members.sql`

**Interfaces:**
- Produces: `public.beta_members(user_id uuid PK → auth.users, granted_at timestamptz, note text)`, RLS: authenticated may SELECT own row only; writes service-role only. Consumed by Task 3's `require_beta` and the frontend waitlist gate (Plan 2).

- [ ] **Step 1: Write the migration**

```sql
-- 2026-07-20-beta-members.sql — invite allowlist for the hosted beta.
-- Grant = one INSERT (service role). Users can read their own row (the SPA
-- waitlist-gate UX); there are deliberately NO insert/update/delete policies.
create table if not exists public.beta_members (
  user_id uuid primary key references auth.users (id) on delete cascade,
  granted_at timestamptz not null default now(),
  note text
);

alter table public.beta_members enable row level security;

create policy "beta members read own row"
  on public.beta_members for select to authenticated
  using ((select auth.uid()) = user_id);
```

- [ ] **Step 2: Apply to the cloud project** — via the Supabase MCP `apply_migration` tool (project `<project-ref>`) or `psql "$DATABASE_URL" -f migrations/2026-07-20-beta-members.sql`.

- [ ] **Step 3: Verify** — `psql "$DATABASE_URL" -c "select relrowsecurity from pg_class where relname='beta_members';"` — Expected: `t`. Then insert your own user id as the first member:
`psql "$DATABASE_URL" -c "insert into public.beta_members (user_id, note) select id, 'founder' from auth.users where email='<your-address>' on conflict do nothing;"`

- [ ] **Step 4: Commit**

```bash
git add migrations/2026-07-20-beta-members.sql
git commit -m "feat(migrations): beta_members invite allowlist (RLS: read own row)"
```

---

### Task 5: Wire the dependencies into the routes

**Files:**
- Modify: `delapan/api/routes_explore.py:29,126-129` (1 call site — uses `request_tenancy_creating`)
- Modify: `delapan/api/routes_findings.py:26,36,42,59,70,81,93` (6 call sites)
- Modify: `delapan/api/routes_kg.py:25,69,81,87,114,139,157,168,198,216` (9 call sites)
- Modify: `delapan/api/routes_canvas.py:26,159,183` (2 call sites — touch ONLY these lines; canvas is the other session's surface)
- Modify: `delapan/api/routes_projects.py:18,29,60,67` (3 `resolve_store()` sites → `request_store`)
- Test: `tests/test_api_auth.py` (append); existing `tests/test_api_routes.py` must stay green untouched.

**Interfaces:**
- Consumes: `request_tenancy`, `request_tenancy_creating`, `request_store` (Task 3).
- Produces: every `/api` route enforces auth when `api.auth == "supabase"`; zero behavior change when `"none"`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_api_auth.py`)

```python
@pytest.fixture()
def supabase_mode_client(monkeypatch, tmp_path):
    """TestClient with api.auth=supabase — requests without/with tokens hit the gate.
    Local SQLite backend keeps it hermetic; tenancy resolution is never reached
    for the 401/403 assertions."""
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("DLP_API__AUTH", "supabase")
    from delapan.core.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    from fastapi.testclient import TestClient

    from delapan.api.main import app

    yield TestClient(app)
    get_settings.cache_clear()
    get_config.cache_clear()


def test_routes_require_token_in_supabase_mode(supabase_mode_client):
    for path in ("/api/projects", "/api/projects/p/kbs/k/findings"):
        r = supabase_mode_client.get(path)
        assert r.status_code == 401, path


def test_routes_403_without_beta_membership(supabase_mode_client, monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([]))
    r = supabase_mode_client.get(
        "/api/projects", headers={"Authorization": f"Bearer {_token('u-no-beta')}"}
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api_auth.py -k "supabase_mode or require_token or without_beta" -v`
Expected: FAIL — routes return 200/404 (no auth enforced yet).

- [ ] **Step 3: Apply the mechanical swap.** Worked example — `routes_explore.py` lines 126-129 today:

```python
@router.post("/explore")
async def explore(project: str, kb: str, body: ExploreBody) -> StreamingResponse:
    ctx, store = resolve_kb_or_404(project, kb)
    return StreamingResponse(_events(ctx, store, body), media_type="text/event-stream")
```

becomes:

```python
@router.post("/explore")
async def explore(
    body: ExploreBody,
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy_creating),
) -> StreamingResponse:
    ctx, store = tenancy
    return StreamingResponse(_events(ctx, store, body), media_type="text/event-stream")
```

with imports swapped accordingly (`from fastapi import APIRouter, Depends`; `from delapan.api.auth import request_tenancy_creating`; drop the now-unused `resolve_kb_or_404` import; `TenantContext`/`Store` are already imported in this file). FastAPI resolves the `{project}`/`{kb}` path params inside the dependency — the route keeps its URL contract.

Apply the identical transform at every listed call site: each `ctx, store = resolve_kb_or_404(project, kb)` becomes a `tenancy: tuple[TenantContext, Store] = Depends(request_tenancy)` parameter (add `from delapan.core.agent.state import TenantContext` / `from delapan.store import Store` where a file lacks them) plus `ctx, store = tenancy` as the first body line; **only `routes_explore.py` uses `request_tenancy_creating`** — all other files use `request_tenancy`. In `routes_projects.py`, each `store = resolve_store()` / inline `resolve_store()` becomes a `store: Store = Depends(request_store)` parameter (Task 3's `request_store`), dropping the `resolve_store` import.

- [ ] **Step 4: Run the full suite** — parity is the gate, not just the new tests.

Run: `uv run pytest`
Expected: new auth tests PASS; every pre-existing test PASSES unchanged (the two known env-related failures on clean master are the only tolerated exceptions — count them first per the test-baseline note).

- [ ] **Step 5: Commit**

```bash
git add delapan/api/routes_explore.py delapan/api/routes_findings.py delapan/api/routes_kg.py \
        delapan/api/routes_canvas.py delapan/api/routes_projects.py tests/test_api_auth.py
git commit -m "feat(api): route tenancy via config-forked auth dependencies"
```

---

### Task 6: Rate limiting

**Files:**
- Modify: `pyproject.toml:25` (add `"slowapi>=0.1.9"` to the `cloud` extra)
- Create: `delapan/api/ratelimit.py`
- Modify: `delapan/api/main.py` (register limiter + handler)
- Modify: `delapan/api/routes_explore.py` (pipeline limit on POST /explore)
- Test: `tests/test_api_auth.py` (append)

**Interfaces:**
- Consumes: `get_config().api.rate_limit_default` / `.rate_limit_pipeline` (Task 1).
- Produces: `limiter: slowapi.Limiter` (key = JWT `sub` when present, else client IP); 429 responses carry `Retry-After`. Applied app-wide by default; explore also carries the pipeline limit.

- [ ] **Step 1: Write the failing test**

```python
def test_rate_limit_pipeline_429(supabase_mode_client, monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    monkeypatch.setenv("DLP_API__RATE_LIMIT_PIPELINE", "1/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    headers = {"Authorization": f"Bearer {_token('u1')}"}
    # Two POSTs: the second must be limited regardless of what the first returns.
    supabase_mode_client.post("/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers)
    r = supabase_mode_client.post(
        "/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    get_config.cache_clear()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_api_auth.py::test_rate_limit_pipeline_429 -v` — Expected: FAIL (no 429).

- [ ] **Step 3: Implement.** `delapan/api/ratelimit.py`:

```python
"""Request rate limiting — slowapi, keyed by JWT subject (fallback: client IP).

    request ──► _key: jwt sub (unverified decode — identity for bucketing only)
                    │ absent/invalid ──► client IP
                    ▼
            Limiter(default = config api.rate_limit_default)
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from delapan.core.config import get_config


def _key(request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        import jwt

        try:  # bucketing identity only — Task 2's verify_bearer does real auth
            return jwt.decode(
                auth.removeprefix("Bearer "), options={"verify_signature": False}
            ).get("sub") or get_remote_address(request)
        except jwt.PyJWTError:
            pass
    return get_remote_address(request)


limiter = Limiter(key_func=_key, default_limits=[lambda: get_config().api.rate_limit_default])


def pipeline_limit() -> str:
    return get_config().api.rate_limit_pipeline
```

`delapan/api/main.py` — after `app = FastAPI(...)`:

```python
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from delapan.api.ratelimit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
```

`routes_explore.py` — decorate the explore route (slowapi needs the `request` param present):

```python
from delapan.api.ratelimit import limiter, pipeline_limit

@router.post("/explore")
@limiter.limit(pipeline_limit)
async def explore(
    request: Request,
    body: ExploreBody,
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy_creating),
) -> StreamingResponse:
    ...
```

(`from fastapi import Request` — add to the file's imports.) Generous defaults mean the local tier never hits the buckets; knobs stay in config per invariant.

- [ ] **Step 4: Run** — `uv run pytest tests/test_api_auth.py -v && uv run pytest` — Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml delapan/api/ratelimit.py delapan/api/main.py delapan/api/routes_explore.py \
        tests/test_api_auth.py
git commit -m "feat(api): slowapi rate limiting keyed by user/IP"
```

---

### Task 7: RLS audit script

**Files:**
- Create: `scripts/rls_audit.py`
- Test: Create `tests/test_rls_audit.py`

**Interfaces:**
- Consumes: `get_settings().database_url`; asyncpg (already in `[cloud]`).
- Produces: `evaluate(tables: list[dict], policies: list[dict]) -> list[str]` (pure; returns human-readable gap strings, empty = clean) and a `python scripts/rls_audit.py` CLI exiting non-zero on gaps. Run against the cloud project before the sign-up link goes public (spec §H).

- [ ] **Step 1: Write the failing test**

```python
"""Pure-function tests for the RLS audit — no database."""
from __future__ import annotations

from scripts.rls_audit import TENANT_TABLES, evaluate


def _table(name, rls=True):
    return {"tablename": name, "rowsecurity": rls}


def _policy(table, cmd, qual="(org_id = something)", with_check=None):
    return {"tablename": table, "cmd": cmd, "qual": qual, "with_check": with_check}


def test_clean_table_passes():
    tables = [_table("findings")]
    policies = [
        _policy("findings", "SELECT"),
        _policy("findings", "INSERT", qual=None, with_check="(org_id = x)"),
    ]
    gaps = [g for g in evaluate(tables, policies) if "findings" in g]
    assert gaps == []


def test_rls_disabled_flagged():
    assert any("rowsecurity" in g for g in evaluate([_table("findings", rls=False)], []))


def test_missing_table_flagged():
    assert any("beta_members" in g for g in evaluate([], []))


def test_insert_without_with_check_flagged():
    tables = [_table("kg_nodes")]
    policies = [
        _policy("kg_nodes", "SELECT"),
        _policy("kg_nodes", "INSERT", qual=None, with_check=None),
    ]
    assert any("WITH CHECK" in g for g in evaluate(tables, policies))


def test_unscoped_qual_flagged():
    tables = [_table("kbs")]
    policies = [_policy("kbs", "SELECT", qual="true")]
    assert any("scope" in g.lower() for g in evaluate(tables, policies))
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_rls_audit.py -v` — Expected: FAIL (`ModuleNotFoundError: scripts.rls_audit`; ensure `scripts/__init__.py` exists — it does).

- [ ] **Step 3: Implement `scripts/rls_audit.py`**

```python
"""RLS audit — every tenant table must be org/user-scoped, by test not convention.

    pg_tables + pg_policies ──► evaluate() ──► gap list (empty = clean)

Catches the kg_schemas class of gap mechanically (spec §H). Read-only catalog
queries — safe against production. Usage: uv run python scripts/rls_audit.py
(requires DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import sys

TENANT_TABLES = {
    "projects", "kbs", "findings", "kg_nodes", "kg_edges", "kg_schemas",
    "resolution_events", "explorations", "beta_members",
    "tracking_initiatives", "tracking_backlog",
}
_SCOPE_MARKERS = ("org_id", "auth.uid()")


def evaluate(tables: list[dict], policies: list[dict]) -> list[str]:
    gaps: list[str] = []
    by_name = {t["tablename"]: t for t in tables}
    for name in sorted(TENANT_TABLES):
        table = by_name.get(name)
        if table is None:
            gaps.append(f"{name}: table not found in public schema")
            continue
        if not table["rowsecurity"]:
            gaps.append(f"{name}: rowsecurity disabled")
        pols = [p for p in policies if p["tablename"] == name]
        if not any(p["cmd"] in ("SELECT", "ALL", "*") for p in pols):
            gaps.append(f"{name}: no SELECT policy")
        for p in pols:
            if p["cmd"] in ("INSERT", "UPDATE", "ALL", "*") and not p.get("with_check"):
                gaps.append(f"{name}: {p['cmd']} policy missing WITH CHECK")
            scope_text = " ".join(filter(None, (p.get("qual"), p.get("with_check"))))
            if scope_text and not any(m in scope_text for m in _SCOPE_MARKERS):
                gaps.append(f"{name}: {p['cmd']} policy not org/user scoped: {scope_text!r}")
    return gaps


async def _fetch() -> tuple[list[dict], list[dict]]:
    import asyncpg  # [cloud] extra

    from delapan.core.config import get_settings

    url = get_settings().database_url
    assert url, "DATABASE_URL required for the RLS audit"
    conn = await asyncpg.connect(url)
    try:
        tables = await conn.fetch(
            "select tablename, rowsecurity from pg_tables where schemaname = 'public'"
        )
        policies = await conn.fetch(
            "select tablename, cmd, qual, with_check from pg_policies "
            "where schemaname = 'public'"
        )
    finally:
        await conn.close()
    return [dict(r) for r in tables], [dict(r) for r in policies]


def main() -> None:
    tables, policies = asyncio.run(_fetch())
    gaps = evaluate(tables, policies)
    for gap in gaps:
        print(f"GAP  {gap}")
    print(f"{len(gaps)} gap(s) across {len(TENANT_TABLES)} tenant tables")
    sys.exit(1 if gaps else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, then the live audit**

Run: `uv run pytest tests/test_rls_audit.py -v` — Expected: PASS ×5.
Run: `uv run python scripts/rls_audit.py` — record the output. **Expected: real gaps are likely** (this is the audit the spec calls for). File each gap as a migration follow-up; do not silence the audit.

- [ ] **Step 5: Commit**

```bash
git add scripts/rls_audit.py tests/test_rls_audit.py
git commit -m "feat(scripts): RLS audit — org-scoped policies verified by test"
```

---

### Task 8: Two-user isolation test

**Files:**
- Test: Create `tests/test_tenant_isolation.py`

**Interfaces:**
- Consumes: `resolve_tenant_for_token` (`mcp/tenancy.py:129`), `tests/fake_supabase.py` fixtures. **Preflight (required):** read `tests/fake_supabase.py` and the fixture usage in `tests/test_curation_store_supabase.py`, and the exact finding-write/list method names in `delapan/store/__init__.py` — use those names verbatim in the test bodies below where `add_finding`/`list_findings` appear as stand-ins.
- Produces: the spec's acceptance-criterion test — user B can neither read nor write user A's data.

- [ ] **Step 1: Write the failing test**

```python
"""Isolation acceptance test (spec §H): two users, two orgs, zero bleed.

Enforcement line under test = the engine's explicit org scoping through the
Store (the service-key path RLS cannot cover)."""
from __future__ import annotations

import pytest

import delapan.mcp.tenancy as tenancy_mod

ORGS = {"user-a": "org-a", "user-b": "org-b"}


@pytest.fixture()
def two_org_cloud(monkeypatch):
    """Cloud backend over fake_supabase, _org_for answering from ORGS.
    Build the store fixture exactly as tests/test_curation_store_supabase.py does."""
    monkeypatch.setattr(tenancy_mod, "_org_for", lambda user_id: ORGS[user_id])
    ...  # fake_supabase-backed get_store wiring per the existing fixture pattern


def test_user_b_cannot_read_user_a_findings(two_org_cloud):
    ctx_a = tenancy_mod.resolve_tenant_for_token("user-a", "tok-a", "proj", "kb", create=True)
    store_a = ...  # get_store("tok-a", org_id=ctx_a.org_id) per fixture
    store_a.add_finding(...)  # exact Store write method per preflight

    ctx_b = tenancy_mod.resolve_tenant_for_token("user-b", "tok-b", "proj", "kb", create=True)
    store_b = ...
    assert store_b.list_findings(ctx_b.kb_id) == []          # no read bleed
    assert ctx_a.org_id != ctx_b.org_id                       # same names, distinct tenants
    assert ctx_a.kb_id != ctx_b.kb_id


def test_user_b_cannot_write_into_user_a_kb(two_org_cloud):
    ctx_a = tenancy_mod.resolve_tenant_for_token("user-a", "tok-a", "proj", "kb", create=True)
    ctx_b = tenancy_mod.resolve_tenant_for_token("user-b", "tok-b", "proj", "kb", create=True)
    store_b = ...
    store_b.add_finding(...)  # write via B's store against B's resolved kb
    store_a = ...
    assert store_a.list_findings(ctx_a.kb_id) == []          # A's KB untouched
```

The `...` markers above are filled during the preflight read — they are fixture
wiring and exact method names, not design gaps; the assertions are the contract.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_tenant_isolation.py -v` — Expected: FAIL (fixture incomplete) → complete wiring → tests must then PASS **without any production credential**.

- [ ] **Step 3: Run the full suite** — `uv run pytest` — Expected: green (± the two known env failures).

- [ ] **Step 4: Commit**

```bash
git add tests/test_tenant_isolation.py
git commit -m "test: two-user tenant isolation acceptance (spec §H)"
```

---

### Task 9: Serve `/api` beside the cloud MCP server

**Files:**
- Modify: `delapan/mcp/cloud_server.py:119-126` (`main()` — serve a combined app)
- Modify: `fly.toml` (`[env]`: add `DLP_API__AUTH = "supabase"`)
- Test: Create `tests/test_cloud_combined_app.py`

**Interfaces:**
- Consumes: `mcp.streamable_http_app()` (FastMCP's Starlette app — MCP stays at its existing path, so the claude.ai connector URL is untouched); `delapan.api.main.app`.
- Produces: `build_combined_app() -> Starlette` — one process serving MCP + `/health` + `/api` on `0.0.0.0:$PORT`.

- [ ] **Step 1: Write the failing test**

```python
"""One Fly process serves MCP and the REST /api (spec §A/§C)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def combined(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_SERVER_URL", "https://test.invalid")
    monkeypatch.setenv("DELAPAN_BACKEND", "local")  # hermetic — app shape is under test
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("DLP_API__AUTH", "supabase")
    from delapan.core.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    from delapan.mcp.cloud_server import build_combined_app

    yield TestClient(build_combined_app())
    get_settings.cache_clear()
    get_config.cache_clear()


def test_health_served(combined):
    assert combined.get("/health").status_code == 200


def test_api_present_and_gated(combined):
    assert combined.get("/api/projects").status_code == 401  # mounted AND auth-gated
```

(If importing `cloud_server` at module scope trips its `CLOUD_SERVER_URL` assert
in other tests, set the env var in this fixture before import — as shown.)

- [ ] **Step 2: Run to verify failure** — Expected: FAIL (`ImportError: build_combined_app`).

- [ ] **Step 3: Implement** — in `delapan/mcp/cloud_server.py`:

```python
def build_combined_app():
    """The Fly-facing ASGI app: FastMCP's streamable-http app (MCP path
    unchanged — the claude.ai connector URL keeps working) with the REST
    /api + /health mounted at root, catch-all last."""
    from starlette.routing import Mount

    from delapan.api.main import app as rest_app

    sapp = mcp.streamable_http_app()
    sapp.router.routes.append(Mount("/", app=rest_app))
    return sapp


def main() -> None:
    from delapan.store import active_backend

    if active_backend() != "cloud":
        raise RuntimeError(
            "cloud_server requires DELAPAN_BACKEND=cloud (or valid Supabase creds present)"
        )
    import uvicorn

    uvicorn.run(build_combined_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
```

`fly.toml` `[env]` gains:

```toml
  DLP_API__AUTH = "supabase"
```

Deploy note (execution step, not code): `fly secrets set SUPABASE_JWT_SECRET=... CORS_ORIGINS=https://delapan.ai` before `fly deploy`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_cloud_combined_app.py -v && uv run pytest` — Expected: green.

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/cloud_server.py fly.toml tests/test_cloud_combined_app.py
git commit -m "feat(cloud): serve authenticated /api beside the MCP server"
```

---

### Task 10: Docs

**Files:**
- Modify: `README.md` ("What's inside" table + "Status & roadmap" — house rule: docs update in the same change)
- Modify: `.env.example` (document `SUPABASE_JWT_SECRET`, `DLP_API__AUTH`; **rebase first** — this file was dirty in the parallel session)

- [ ] **Step 1: README** — add a "What's inside" row for `api/auth.py` ("config-forked bearer auth + beta gate for the public /api") and a roadmap line marking hosted-tier phase 1 (backend auth) done, pointing at `docs/truenorth/specs/2026-07-20-public-release-design.md`.
- [ ] **Step 2: .env.example** — under the Supabase block add `SUPABASE_JWT_SECRET=` with a one-line comment ("verifies /api bearer tokens locally; Supabase dashboard → Settings → API → JWT secret") and `# DLP_API__AUTH=supabase  (cloud deploys only — local stays auth-less)`.
- [ ] **Step 3: Commit**

```bash
git add README.md .env.example
git commit -m "docs: hosted-tier /api auth — README + env reference"
```

---

## Self-review notes (already applied)

- **Spec coverage (Plan 1 scope = spec §C, §H, backend halves of §B/§E):** auth dependency ✔ (T2/T3), org scoping ✔ (T3/T5/T8), beta_members ✔ (T4), config knobs ✔ (T1), rate limiting ✔ (T6), RLS audit ✔ (T7), isolation acceptance ✔ (T8), cloud serving + CORS/secrets ✔ (T9), docs ✔ (T10). Loud-failure fixes and the key-gate fix are **omitted deliberately** — already landed by the parallel canvas session (verify still true at execution preflight). Frontend, landing, legal, SMTP/Turnstile/Sentry/UptimeRobot/PostHog, onboarding = Plans 2–3.
- **Placeholders:** Task 8 contains explicit `...` fixture markers by design, resolved by its mandatory preflight read (fake_supabase fixture + exact Store method names) — the assertions themselves are complete. No other TBDs.
- **Type consistency:** `request_tenancy`/`request_tenancy_creating` return `tuple[TenantContext, Store]` everywhere; `request_store` returns `Store`; `verify_bearer` returns `str`; `evaluate(tables, policies) -> list[str]` matches its tests.
