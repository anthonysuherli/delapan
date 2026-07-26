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
from .onboarding import kb_not_found_card
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
    stateless_http=True,
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
    """Inject KB context into THIS conversation. Returns ``{"banner", "preamble",
    "coverage"}``: the delapan wordmark to lead the message with, the <preamble>
    (synopsis spine plus query-relevant findings), and the coverage band for the
    query (``rich``/``sparse``/``gap``). Same contract as the local
    ``delapan_resume`` tool, scoped to the authenticated claude.ai caller's org."""
    caller = _caller()
    try:
        ctx = resolve_tenant_for_token(caller.subject, caller.token, project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — onboarding card for a missing project/KB
        return kb_not_found_card(
            project, kb, exc, store=resolve_store_for_token(caller.subject, caller.token)
        )
    return await _resume_impl(ctx, query, depth)


@mcp.tool()
async def delapan_search(project: str, kb: str, query: str, limit: int | None = None) -> dict:
    """Recall from the KB only — semantic search over existing findings, no web.
    Returns ``{"query", "findings"}`` with the ranked finding rows (each carries a
    ``similarity``). Same contract as the local ``delapan_search`` tool, scoped to
    the authenticated claude.ai caller's org."""
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
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating it on demand). Blocks until complete (may
    take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "finding_ids", "count", "synopsis"}`` — ``synopsis`` is the
    rebuild status (``"rebuilt"``/``"skipped"``/``"failed: <msg>"``). Same contract
    as the local ``delapan_explore`` tool, scoped to the authenticated claude.ai
    caller's org."""
    caller = _caller()
    ctx = resolve_tenant_for_token(caller.subject, caller.token, project, kb, create=True)
    return await _explore_impl(ctx, prompt, max_findings)


@mcp.tool()
async def delapan_projects() -> dict:
    """List the authenticated caller's projects (by name) with their KBs — for
    client discovery. Returns ``{"projects": [...]}``. Same contract as the local
    ``delapan_projects`` tool."""
    caller = _caller()
    store = resolve_store_for_token(caller.subject, caller.token)
    return _projects_impl(store)


class _CombinedApp:
    """Dispatches each ASGI request to the MCP app or the REST app by
    path-probing the MCP app's own route table (Starlette ``BaseRoute.matches``
    — read-only, no side effects) rather than nesting one Starlette app inside
    another via ``Mount``.

    Why not just ``sapp.router.routes.append(Mount("/", app=rest_app))``
    (the previous approach): ``mcp.streamable_http_app()`` returns a
    ``Starlette`` instance whose ``AuthenticationMiddleware(BearerAuthBackend
    (SupabaseTokenVerifier()))`` is app-wide — it wraps that Starlette
    instance's ENTIRE router (``Starlette.build_middleware_stack`` wraps
    ``self.router`` once, uniformly), regardless of how routes are appended to
    it. Appending a REST catch-all ``Mount`` to that same router's route list
    still routes REST requests through the MCP app's own middleware first,
    forcing a synchronous GoTrue ``client.auth.get_user(token)`` round-trip
    (plus a fresh ``create_client()``) in front of every Bearer-carrying
    ``/api`` request — contradicting ``delapan/api/auth.py``'s "no GoTrue
    round-trip per request" docstring, and blocking the event loop.

    Why not two nested ``Mount``s either: FastMCP's own route lives at the
    *absolute* path ``/mcp`` (``mcp.settings.streamable_http_path``), and a
    Starlette ``Mount`` strips its own prefix before dispatching into the
    child app — mounting the MCP app at ``Mount("/mcp", app=mcp_app)`` would
    require a client to hit ``/mcp/mcp`` for the inner match to succeed,
    breaking the exact ``/mcp`` URL the live claude.ai connector uses.
    ``Mount("/", app=mcp_app)`` avoids that but then swallows every path
    (its compiled regex matches any string), so the REST app would never
    be reached.

    This class sidesteps both: it probes ``mcp_app.routes`` — via each
    route's own ``.matches(scope)`` — to decide ownership, then calls
    whichever app owns the request directly (unmounted, full pass-through),
    so neither app's own path scheme or middleware stack needs to change.
    """

    def __init__(self, mcp_app, rest_app) -> None:
        self._mcp_app = mcp_app
        self._rest_app = rest_app

    async def __call__(self, scope, receive, send) -> None:
        # Lifespan: only the MCP app owns startup/shutdown work (the
        # streamable-http session manager); the REST app registers none.
        if scope["type"] == "lifespan" or self._is_mcp_request(scope):
            await self._mcp_app(scope, receive, send)
        else:
            await self._rest_app(scope, receive, send)

    def _is_mcp_request(self, scope) -> bool:
        from starlette.routing import Match

        for route in self._mcp_app.routes:
            match, _ = route.matches(scope)
            if match != Match.NONE:
                return True
        return False


def build_combined_app():
    """The Fly-facing ASGI app: FastMCP's streamable-http app (MCP path
    unchanged — the claude.ai connector URL keeps working, and its own
    Bearer-auth middleware stays scoped to /mcp + its auth-metadata routes)
    dispatched alongside the REST /api + /health app, which sits entirely
    outside that middleware — see ``_CombinedApp`` for why a plain ``Mount``
    can't do this."""
    from delapan.api.main import app as rest_app

    mcp_app = mcp.streamable_http_app()
    return _CombinedApp(mcp_app, rest_app)


def main() -> None:
    from delapan.store import active_backend

    if active_backend() != "cloud":
        raise RuntimeError(
            "cloud_server requires DELAPAN_BACKEND=cloud (or valid Supabase creds present)"
        )
    import uvicorn

    uvicorn.run(build_combined_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
