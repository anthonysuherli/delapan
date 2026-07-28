# Cloud-hosted remote MCP server for claude.ai — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make delapan's four MCP tools reachable from claude.ai over `streamable-http`, authenticated via Supabase Auth's OAuth 2.1 server, with zero behavior change to the existing local stdio server.

**Architecture:** A new `delapan/mcp/cloud_server.py` entrypoint runs the same tool logic as `delapan/mcp/server.py` over `streamable-http` instead of stdio. The `mcp` SDK (v1.28.1, already installed) handles the OAuth resource-server handshake (`401`/`WWW-Authenticate`, protected-resource metadata) natively via `FastMCP(auth=AuthSettings(...), token_verifier=...)` — no hand-written endpoints. A new `SupabaseTokenVerifier` resolves claude.ai's bearer token to a Supabase user; two new additive functions in `tenancy.py` (`resolve_tenant_for_token`/`resolve_store_for_token`) route that identity into the existing RLS-scoped `SupabaseStore` path, bypassing only the fixed-user password-grant login (`_login()`) that the local stdio path still uses unchanged. `server.py`'s four tool bodies are split into a thin `@mcp.tool()` wrapper (unchanged) plus a shared `_*_impl` function, so `cloud_server.py` reuses the real logic instead of duplicating it.

**Tech Stack:** Python 3.12, `mcp>=1.16.0` (installed: 1.28.1 — `AuthSettings`/`TokenVerifier`/`streamable-http` support verified locally, see Task 4), `supabase-py`, Fly.io, Docker, pytest.

**Spec:** `backend/docs/superpowers/specs/2026-07-17-cloud-remote-mcp-design.md` (see the two "Correction" notes in §5/§6 — this plan implements the corrected design, not the original file table).

## Global Constraints

- **All commands run from `backend/`** (the delapan git repo). Use `.venv/bin/python` / `.venv/bin/pytest`.
- **Never change the local stdio server's behavior.** Every existing `@mcp.tool()` wrapper in `server.py` must keep calling `resolve_tenant`/`resolve_store` exactly as today; `tests/test_mcp_smoke.py` must keep passing unmodified throughout.
- **No new dependencies.** `mcp`, `supabase`, `uvicorn` are already in `pyproject.toml` (base + `[cloud]` extra); `AuthSettings`/`TokenVerifier`/`streamable-http` are already present in the installed `mcp` version.
- **Supabase project host** comes from `Settings.supabase_url` (`SUPABASE_URL` env) at runtime — never hardcode the project ref (`<project-ref>`) in `delapan/mcp/*` (it's already hardcoded once, intentionally, inside the migration script from the sibling plan — don't add a second hardcoded copy here).
- **`DELAPAN_BACKEND=cloud`** is a required deployment env var for the cloud server (set in `fly.toml`, not mutated by application code) — `cloud_server.main()` fails fast if it isn't set.
- **The cloud server does not need `DLP_MCP_USER_EMAIL`/`DLP_MCP_USER_PASSWORD`.** Those exist only for `_login()`'s password grant, which the token-based path never calls.

---

### Task 1: `tenancy.py` — token-accepting resolution functions

**Files:**
- Modify: `delapan/mcp/tenancy.py`
- Test: `tests/test_tenancy.py` (extend)

**Interfaces:**
- Produces:
  - `resolve_tenant_for_token(user_id: str, access_token: str, project: str, kb: str, *, create: bool = True) -> TenantContext`
  - `resolve_store_for_token(user_id: str, access_token: str)` — returns a `Store`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenancy.py`:

```python
def test_resolve_tenant_for_token_skips_login(monkeypatch):
    from delapan.mcp import tenancy

    monkeypatch.setattr(tenancy, "_org_for", lambda user_id: "org-123")

    calls = {}

    class FakeStore:
        def resolve_project(self, name, *, create):
            calls["resolve_project"] = (name, create)
            return "org-123", "proj-1"

        def resolve_kb(self, org_id, project_id, name, *, create):
            calls["resolve_kb"] = (org_id, project_id, name, create)
            return "kb-1"

    def fake_get_store(token, *, org_id):
        calls["get_store"] = (token, org_id)
        return FakeStore()

    monkeypatch.setattr("delapan.store.get_store", fake_get_store)

    ctx = tenancy.resolve_tenant_for_token("user-9", "tok-abc", "demo", "main", create=False)

    assert ctx.user_id == "user-9"
    assert ctx.org_id == "org-123"
    assert ctx.project_id == "proj-1"
    assert ctx.kb_id == "kb-1"
    assert ctx.access_token == "tok-abc"
    assert calls["get_store"] == ("tok-abc", "org-123")
    assert calls["resolve_project"] == ("demo", False)
    assert calls["resolve_kb"] == ("org-123", "proj-1", "main", False)


def test_resolve_store_for_token_skips_login(monkeypatch):
    from delapan.mcp import tenancy

    monkeypatch.setattr(tenancy, "_org_for", lambda user_id: "org-123")
    sentinel = object()
    monkeypatch.setattr(
        "delapan.store.get_store",
        lambda token, *, org_id: sentinel if (token, org_id) == ("tok-abc", "org-123") else None,
    )

    store = tenancy.resolve_store_for_token("user-9", "tok-abc")
    assert store is sentinel
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_tenancy.py -v`
Expected: `AttributeError: module 'delapan.mcp.tenancy' has no attribute 'resolve_tenant_for_token'`

- [ ] **Step 3: Implement**

Append to `delapan/mcp/tenancy.py` (after `resolve_store`):

```python
def resolve_tenant_for_token(
    user_id: str, access_token: str, project: str, kb: str, *, create: bool = True
) -> TenantContext:
    """Cloud-only: resolve tenancy for an externally supplied (user_id, access_token)
    pair — e.g. claude.ai's OAuth token — skipping ``_login()``'s password grant.
    The token-aware sibling of ``resolve_tenant``'s cloud branch."""
    from delapan.store import get_store

    org_id = _org_for(user_id)
    store = get_store(access_token, org_id=org_id)
    org_id, project_id = store.resolve_project(project, create=create)
    kb_id = store.resolve_kb(org_id, project_id, kb, create=create)
    return TenantContext(
        user_id=user_id,
        org_id=org_id,
        project_id=project_id,
        kb_id=kb_id,
        thread_id=str(uuid.uuid4()),
        access_token=access_token,
    )


def resolve_store_for_token(user_id: str, access_token: str):
    """Cloud-only: org-scoped Store for an externally supplied token — the
    token-aware sibling of ``resolve_store``."""
    from delapan.store import get_store

    return get_store(access_token, org_id=_org_for(user_id))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tenancy.py -v`
Expected: `3 passed` (the pre-existing local test plus the two new ones).

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/tenancy.py tests/test_tenancy.py
git commit -m "feat: add token-accepting tenancy resolution for the cloud MCP server"
```

---

### Task 2: `cloud_auth.py` — Supabase-backed token verifier

**Files:**
- Create: `delapan/mcp/cloud_auth.py`
- Test: `tests/test_cloud_auth.py`

**Interfaces:**
- Consumes: `delapan.core.clients.supabase.create_client` (existing), `delapan.core.config.get_settings` (existing).
- Produces: `SupabaseTokenVerifier` — a `mcp.server.auth.provider.TokenVerifier` with `async def verify_token(self, token: str) -> AccessToken | None`. `AccessToken.subject` carries the resolved Supabase `user_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cloud_auth.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.mcp.cloud_auth import SupabaseTokenVerifier


class _FakeAuth:
    def __init__(self, user_id: str | None):
        self._user_id = user_id

    def get_user(self, token):
        if self._user_id is None:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=self._user_id))


@pytest.mark.asyncio
async def test_verify_token_valid_returns_access_token():
    verifier = SupabaseTokenVerifier(
        client_factory=lambda token: SimpleNamespace(auth=_FakeAuth("user-9"))
    )
    result = await verifier.verify_token("tok-abc")
    assert result is not None
    assert result.subject == "user-9"
    assert result.token == "tok-abc"


@pytest.mark.asyncio
async def test_verify_token_invalid_returns_none():
    verifier = SupabaseTokenVerifier(
        client_factory=lambda token: SimpleNamespace(auth=_FakeAuth(None))
    )
    result = await verifier.verify_token("bad-token")
    assert result is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_cloud_auth.py -v`
Expected: `ModuleNotFoundError: No module named 'delapan.mcp.cloud_auth'`

- [ ] **Step 3: Implement**

Create `delapan/mcp/cloud_auth.py`:

```python
"""Resource-server token verification for the cloud MCP server.

    claude.ai bearer token ──► SupabaseTokenVerifier.verify_token ──► AccessToken
                                        │ auth.get_user(token) against Supabase

Supabase Auth is the OAuth 2.1 authorization server (see docs/superpowers/specs/
2026-07-17-cloud-remote-mcp-design.md §6); this only validates the token it
issued and extracts the user id TenantContext resolution needs downstream.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.auth.provider import AccessToken, TokenVerifier


def _default_client(_token: str) -> Any:
    from delapan.core.clients.supabase import create_client
    from delapan.core.config import get_settings

    s = get_settings()
    return create_client(s.supabase_url, s.supabase_anon_key)


class SupabaseTokenVerifier(TokenVerifier):
    """Validates a bearer token by asking Supabase who it belongs to."""

    def __init__(self, client_factory: Callable[[str], Any] = _default_client) -> None:
        self._client_factory = client_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        client = self._client_factory(token)
        try:
            res = client.auth.get_user(token)
        except Exception:  # noqa: BLE001 — any auth failure means an invalid token
            return None
        if not res or not res.user:
            return None
        return AccessToken(token=token, client_id="claude.ai", scopes=[], subject=res.user.id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cloud_auth.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/cloud_auth.py tests/test_cloud_auth.py
git commit -m "feat: add SupabaseTokenVerifier for the cloud MCP resource server"
```

---

### Task 3: Extract shared tool implementations in `server.py`

Behavior-preserving refactor: split each `@mcp.tool()` body into the unchanged tenancy-resolution/error-handling wrapper plus a new `_*_impl` function holding everything after tenancy resolution. The local stdio server's behavior does not change — this task's own verification is that the existing smoke test still passes untouched.

**Files:**
- Modify: `delapan/mcp/server.py`
- Test: `tests/test_mcp_smoke.py` (existing — must pass unmodified; no new test needed since this is a pure refactor with no new behavior)

**Interfaces:**
- Produces (importable by Task 5's `cloud_server.py`):
  - `_resume_impl(ctx: TenantContext, query: str | None, depth: Depth) -> dict`
  - `_search_impl(ctx: TenantContext, query: str, limit: int | None) -> dict`
  - `_explore_impl(ctx: TenantContext, prompt: str, max_findings: int | None) -> dict`
  - `_projects_impl(store) -> dict`

- [ ] **Step 1: Confirm the baseline passes**

Run: `.venv/bin/pytest tests/test_mcp_smoke.py -v`
Expected: `1 passed` (before touching anything — this is the regression baseline for Step 3).

- [ ] **Step 2: Rewrite `server.py`**

Replace the full contents of `delapan/mcp/server.py` with:

```python
"""FastMCP stdio server — the open-core plugin's entry path into delapan.

    MCP tool call ──► resolve_tenant(project, kb) ──► get_store() ──► engine

A third entry path alongside the (cloud-only) HTTP API; it drains the same engine
through the Store seam, so one engine serves both tiers. The surface is
deliberately small — four tools:

    delapan_resume    — inject KB context (banner + preamble + coverage)
    delapan_search    — semantic search over existing findings
    delapan_explore   — run the research pipeline + persist findings
    delapan_projects  — list the caller's projects/KBs

Each tool is a thin tenancy-resolution wrapper around a shared ``_*_impl``
function; ``delapan/mcp/cloud_server.py`` reuses those same ``_*_impl``
functions with claude.ai-token-based tenancy resolution instead of this
module's fixed-configured-user login, so the two entrypoints share one copy
of the actual tool logic.

Run with: ``python -m delapan.mcp.server``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from delapan.core.agent.preamble import Depth, select_preamble
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.agent.state import TenantContext
from delapan.core.clients.embeddings import embed_text
from delapan.core.config import get_config, get_settings
from delapan.core.exploration import run_exploration
from delapan.core.knowledge_graph.builder import schedule_kg_update
from delapan.core.memory.persist import resolve_and_persist
from delapan.store import get_store

from .banner import DELAPAN_BANNER
from .tenancy import resolve_store, resolve_tenant

logger = logging.getLogger(__name__)

mcp = FastMCP("delapan")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Inject → this conversation --------------------------------------------


async def _resume_impl(ctx: TenantContext, query: str | None, depth: Depth) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    preamble, coverage = await select_preamble(query, store=store, kb_id=ctx.kb_id, depth=depth)
    return {"banner": DELAPAN_BANNER, "preamble": preamble, "coverage": coverage}


@mcp.tool()
async def delapan_resume(
    project: str, kb: str, query: str | None = None, depth: Depth = "normal"
) -> dict:
    """Inject KB context into THIS conversation. Returns ``{"banner", "preamble",
    "coverage"}``: the delapan wordmark to lead the message with, the <preamble>
    (synopsis spine plus query-relevant findings), and the coverage band for the
    query (``rich``/``sparse``/``gap``). The same rendering + signal the cloud
    ``/v1/preamble`` serves to apps (both go through ``select_preamble``)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _resume_impl(ctx, query, depth)


# --- Recall ----------------------------------------------------------------


async def _search_impl(ctx: TenantContext, query: str, limit: int | None) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    emb = await embed_text(query)
    hits = await store.match_findings(ctx.kb_id, emb, match_count=limit or 10, min_similarity=0.0)
    return {"query": query, "findings": hits}


@mcp.tool()
async def delapan_search(project: str, kb: str, query: str, limit: int | None = None) -> dict:
    """Recall from the KB only — semantic search over existing findings, no web.
    Returns ``{"query", "findings"}`` with the ranked finding rows (each carries a
    ``similarity``)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _search_impl(ctx, query, limit)


# --- Build the KB ------------------------------------------------------------


async def _explore_impl(ctx: TenantContext, prompt: str, max_findings: int | None) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().exploration
    cap = min(max_findings or cfg.default_max_findings, cfg.max_findings)

    exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, prompt)
    try:
        findings = await run_exploration(
            prompt,
            exploration_id=exp_id,
            project_id=ctx.project_id,
            kb_id=ctx.kb_id,
            cfg=cfg,
        )
        captured = findings[:cap]

        outcome = await resolve_and_persist(ctx, store, captured, get_config())
        ids = outcome.affected_finding_ids

        store.update_exploration(
            exp_id, status="completed", completed_at=_now_iso(), finding_ids=ids
        )
        # Grow the stable layers from the new findings. Synopsis rebuild is awaited
        # (best-effort, never raises); the KG update is fire-and-forget (sync
        # scheduler, gated on an approved intent schema — no-op otherwise).
        await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise

    return {"exploration_id": exp_id, "finding_ids": ids, "count": len(ids)}


@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str, max_findings: int | None = None
) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating the project/KB on demand). Blocks until
    complete (may take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "finding_ids", "count"}``."""
    ctx = resolve_tenant(project, kb, create=True)
    return await _explore_impl(ctx, prompt, max_findings)


# --- Tenancy ---------------------------------------------------------------


def _projects_impl(store) -> dict:
    return {"projects": store.list_projects()}


@mcp.tool()
async def delapan_projects() -> dict:
    """List the caller's projects (by name) with their KBs — for client discovery.
    Returns ``{"projects": [...]}``."""
    store = resolve_store()
    return _projects_impl(store)


def main() -> None:
    get_settings()  # fail fast if infra env is missing
    mcp.run()


if __name__ == "__main__":
    main()
```

The only substantive change from the original file: each tool's body after tenancy resolution moved into a module-level `_*_impl` function; the `@mcp.tool()` wrappers call it. Tool signatures, docstrings, error handling, and control flow are otherwise identical. One import added: `from delapan.core.agent.state import TenantContext` (for the `_*_impl` type hints).

- [ ] **Step 3: Run the smoke test to verify zero regression**

Run: `.venv/bin/pytest tests/test_mcp_smoke.py -v`
Expected: `1 passed` — identical result to Step 1. If this fails, the extraction changed behavior; diff against the original body logic line-by-line before proceeding.

- [ ] **Step 4: Run the full test suite as a broader regression check**

Run: `.venv/bin/pytest -v`
Expected: same pass count as before this task started (no new failures introduced by the refactor).

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/server.py
git commit -m "refactor: extract shared tool impls from server.py for cloud_server.py reuse"
```

---

### Task 4: `cloud_server.py` — the streamable-http entrypoint

**Files:**
- Create: `delapan/mcp/cloud_server.py`
- Test: `tests/test_cloud_server.py`

**Interfaces:**
- Consumes: `_resume_impl`/`_search_impl`/`_explore_impl`/`_projects_impl` (Task 3), `resolve_tenant_for_token`/`resolve_store_for_token` (Task 1), `SupabaseTokenVerifier` (Task 2).
- Produces: module-level `mcp: FastMCP` (four tools registered) and `main()`.

**Note on `AuthSettings`/`TokenVerifier` behavior:** verified locally before writing this task — `FastMCP(token_verifier=..., auth=AuthSettings(issuer_url=..., resource_server_url=...))` serves `/.well-known/oauth-protected-resource` and returns a spec-correct `401` + `WWW-Authenticate: Bearer ... resource_metadata="..."` automatically; nothing to hand-write. Step 5 below reproduces that verification against this task's own module.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cloud_server.py`:

```python
from __future__ import annotations

import importlib

import pytest


def _reload_cloud_server(monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "cloud")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    monkeypatch.setenv("CLOUD_SERVER_URL", "https://delapan-cloud.example")
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()

    import delapan.mcp.cloud_server as cs

    importlib.reload(cs)
    return cs


@pytest.mark.asyncio
async def test_registers_four_tools(monkeypatch):
    cs = _reload_cloud_server(monkeypatch)
    tools = await cs.mcp.list_tools()
    assert {t.name for t in tools} == {
        "delapan_resume", "delapan_search", "delapan_explore", "delapan_projects",
    }


def test_auth_points_at_supabase_issuer(monkeypatch):
    cs = _reload_cloud_server(monkeypatch)
    assert str(cs.mcp.settings.auth.issuer_url) == "https://x.supabase.co/auth/v1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_cloud_server.py -v`
Expected: `ModuleNotFoundError: No module named 'delapan.mcp.cloud_server'`

- [ ] **Step 3: Implement**

Create `delapan/mcp/cloud_server.py`:

```python
"""streamable-http MCP server — the claude.ai cloud entry path into delapan.

    claude.ai (OAuth bearer) ──► SupabaseTokenVerifier ──► resolve_*_for_token ──► engine

Reuses the same tool implementations as the local stdio server
(delapan/mcp/server.py) via the shared ``_*_impl`` functions; only tenancy
resolution differs — the caller's own Supabase OAuth token is used instead of
a fixed configured MCP user. Supabase Auth is the OAuth 2.1 authorization
server; this process is a resource server only (see docs/superpowers/specs/
2026-07-17-cloud-remote-mcp-design.md).

Run with: ``python -m delapan.mcp.cloud_server``. Requires DELAPAN_BACKEND=cloud,
SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY, and CLOUD_SERVER_URL
(this server's own public HTTPS URL) in the environment.
"""

from __future__ import annotations

import logging
import os

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from delapan.core.agent.preamble import Depth
from delapan.core.config import get_settings

from .cloud_auth import SupabaseTokenVerifier
from .server import _explore_impl, _projects_impl, _resume_impl, _search_impl
from .tenancy import resolve_store_for_token, resolve_tenant_for_token

logger = logging.getLogger(__name__)

_cloud_server_url = os.environ.get("CLOUD_SERVER_URL")
assert _cloud_server_url, "CLOUD_SERVER_URL must be set to this server's own public HTTPS URL"

_settings = get_settings()
assert _settings.supabase_url, "SUPABASE_URL must be set for the cloud MCP server"

mcp = FastMCP(
    "delapan-cloud",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    token_verifier=SupabaseTokenVerifier(),
    auth=AuthSettings(
        issuer_url=f"{_settings.supabase_url}/auth/v1",
        resource_server_url=_cloud_server_url,
    ),
)


def _caller() -> AccessToken:
    token = get_access_token()
    if token is None:  # pragma: no cover — token_verifier already rejects this
        raise RuntimeError("no authenticated caller")
    return token


@mcp.tool()
async def delapan_resume(
    project: str, kb: str, query: str | None = None, depth: Depth = "normal"
) -> dict:
    """Inject KB context into THIS conversation. Same contract as the local
    ``delapan_resume`` tool, scoped to the authenticated claude.ai caller's org."""
    caller = _caller()
    try:
        ctx = resolve_tenant_for_token(caller.subject, caller.token, project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _resume_impl(ctx, query, depth)


@mcp.tool()
async def delapan_search(project: str, kb: str, query: str, limit: int | None = None) -> dict:
    """Recall from the KB only — semantic search over existing findings, no web.
    Same contract as the local ``delapan_search`` tool."""
    caller = _caller()
    try:
        ctx = resolve_tenant_for_token(caller.subject, caller.token, project, kb, create=False)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _search_impl(ctx, query, limit)


@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str, max_findings: int | None = None
) -> dict:
    """Run the research pipeline and persist findings to the named KB (creating
    it on demand). Same contract as the local ``delapan_explore`` tool."""
    caller = _caller()
    ctx = resolve_tenant_for_token(caller.subject, caller.token, project, kb, create=True)
    return await _explore_impl(ctx, prompt, max_findings)


@mcp.tool()
async def delapan_projects() -> dict:
    """List the authenticated caller's projects/KBs. Same contract as the local
    ``delapan_projects`` tool."""
    caller = _caller()
    store = resolve_store_for_token(caller.subject, caller.token)
    return _projects_impl(store)


def main() -> None:
    from delapan.store import active_backend

    if active_backend() != "cloud":
        raise RuntimeError(
            "cloud_server requires DELAPAN_BACKEND=cloud (or valid Supabase creds present)"
        )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cloud_server.py -v`
Expected: `2 passed`

- [ ] **Step 5: Manually verify the auth handshake against a live local instance**

```bash
CLOUD_SERVER_URL=https://example-cloud-server.fly.dev \
SUPABASE_URL=https://<project-ref>.supabase.co \
SUPABASE_ANON_KEY=<your anon key> \
SUPABASE_SERVICE_ROLE_KEY=<your service role key> \
DELAPAN_BACKEND=cloud \
PORT=8931 \
.venv/bin/python -m delapan.mcp.cloud_server &
sleep 2
curl -sD - -o /dev/null http://127.0.0.1:8931/mcp \
  -X POST -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
curl -s http://127.0.0.1:8931/.well-known/oauth-protected-resource
kill %1
```

Expected: the first `curl` prints `HTTP/1.1 401 Unauthorized` with a `www-authenticate: Bearer error="invalid_token", ... resource_metadata="https://example-cloud-server.fly.dev/.well-known/oauth-protected-resource"` header; the second prints `{"resource":"https://example-cloud-server.fly.dev/","authorization_servers":["https://<project-ref>.supabase.co/auth/v1"],"bearer_methods_supported":["header"]}`.

- [ ] **Step 6: Commit**

```bash
git add delapan/mcp/cloud_server.py tests/test_cloud_server.py
git commit -m "feat: add streamable-http cloud MCP server with Supabase OAuth"
```

---

### Task 5: Dockerfile + fly.toml

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `fly.toml`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY delapan ./delapan

RUN pip install --no-cache-dir -e ".[cloud]"

EXPOSE 8000

CMD ["python", "-m", "delapan.mcp.cloud_server"]
```

- [ ] **Step 2: Write `.dockerignore`**

```
.venv/
.git/
.worktrees/
__pycache__/
*.pyc
tests/
scripts/
docs/
.env
```

- [ ] **Step 3: Write `fly.toml`**

```toml
app = "delapan-cloud-mcp"
primary_region = "sjc"

[build]

[env]
  DELAPAN_BACKEND = "cloud"
  PORT = "8000"
  CLOUD_SERVER_URL = "https://delapan-cloud-mcp.fly.dev"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false
  auto_start_machines = true
  min_machines_running = 1

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
```

`app`/`primary_region`/the `fly.dev` hostname in `CLOUD_SERVER_URL` get finalized by `fly launch` in Task 6 Step 1 (it may rename the app if `delapan-cloud-mcp` is taken) — treat these three values as placeholders to reconcile against whatever `fly launch` actually assigns.

- [ ] **Step 4: Verify the image builds locally**

Run: `docker build -t delapan-cloud-mcp .`
Expected: build succeeds, ending with `Successfully tagged delapan-cloud-mcp:latest` (or the equivalent BuildKit success output).

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore fly.toml
git commit -m "chore: add Docker + Fly.io deploy config for the cloud MCP server"
```

---

### Task 6: Deploy to Fly.io and verify live

**Files:** none (operational).
**Prerequisite:** a Fly.io account and `flyctl` installed and authenticated (`flyctl auth login`) — not yet set up per the spec's risks section; do this before Step 1 if needed.

- [ ] **Step 1: Launch the app**

Run: `flyctl launch --no-deploy --copy-config`
Expected: creates the Fly app (adjust `fly.toml`'s `app`/`primary_region`/`CLOUD_SERVER_URL` to match whatever name/region `flyctl` assigns, then commit that adjustment).

- [ ] **Step 2: Set secrets**

```bash
flyctl secrets set \
  SUPABASE_URL=https://<project-ref>.supabase.co \
  SUPABASE_ANON_KEY=<from backend/.env> \
  SUPABASE_SERVICE_ROLE_KEY=<from backend/.env> \
  ANTHROPIC_API_KEY=<from backend/.env, if set> \
  OPENAI_API_KEY=<from backend/.env, if set> \
  AI_GATEWAY_API_KEY=<from backend/.env, if set> \
  TAVILY_API_KEY=<from backend/.env>
```
Set whichever LLM-related keys your `backend/.env` actually has configured (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`AI_GATEWAY_API_KEY`+`AI_GATEWAY_BASE_URL`) — these are `delapan_explore`'s dependencies, not new to this deployment. Do **not** set `DLP_MCP_USER_EMAIL`/`DLP_MCP_USER_PASSWORD` — the cloud server never calls `_login()`.

- [ ] **Step 3: Deploy**

Run: `flyctl deploy`
Expected: build + deploy succeeds; `flyctl status` shows one machine in `started` state.

- [ ] **Step 4: Verify the auth handshake against the deployed instance**

```bash
curl -sD - -o /dev/null https://<your-app>.fly.dev/mcp \
  -X POST -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```
Expected: `HTTP/1.1 401 Unauthorized` with the same `www-authenticate` shape verified locally in Task 4 Step 5, now pointing at the real `fly.dev` host.

- [ ] **Step 5: Run the env-gated live smoke test**

Extend `tests/test_supabase_live.py` (only if `RUN_CLOUD_TESTS=1` live tests are being used as the ongoing regression check for this server — otherwise this step is covered by the manual acceptance in Task 7). At minimum, confirm the deployed server itself is healthy:

Run: `flyctl logs` and confirm no repeated crash-loop restarts after the first request.

- [ ] **Step 6: Commit any `fly.toml` adjustments from Step 1**

```bash
git add fly.toml
git commit -m "chore: finalize fly.toml app name/region from flyctl launch"
```
(Skip this commit if Step 1 required no changes.)

---

### Task 7: Manual acceptance — connect from claude.ai

**Files:** none (manual verification in the browser).

- [ ] **Step 1: Add the custom connector**

In claude.ai: Settings → Connectors → Add custom connector → enter `https://<your-app>.fly.dev/mcp`. Complete the OAuth consent screen when claude.ai redirects to Supabase's login.

- [ ] **Step 2: Run each tool once against the `demo` KB**

In a claude.ai conversation with the connector enabled, ask Claude to call:
- `delapan_projects` — expect `demo`, `qwen-hackathon`, and `actuary` (with its 4 KBs) in the list.
- `delapan_resume(project="demo", kb="main")` — expect a `banner`/`preamble`/`coverage` response.
- `delapan_search(project="demo", kb="main", query="<any term likely in those 28 findings>")` — expect non-empty `findings`.
- `delapan_explore(project="demo", kb="main", prompt="<a small research prompt>")` — expect a completed `exploration_id`/`finding_ids`/`count` response. (If Tavily is over its plan quota — see the spec's risks section — this may silently return `count: 0`; that's the pre-existing bug, not a regression from this project.)

Expected: all four calls succeed without a `401` or connection error, confirming the full OAuth → resource-server → RLS-scoped-store path works end-to-end from claude.ai itself.
