"""Bearer auth + beta gating for the public /api surface.

    Authorization: Bearer <supabase JWT>
        │ api.auth == "none"     ──► resolve_kb_or_404 / resolve_store (unchanged)
        └ api.auth == "supabase" ──► verify_bearer (pyjwt HS256, aud="authenticated")
                                       ──► require_beta (beta_members via service client)
                                       ──► resolve_*_for_token ──► (ctx, org-scoped store)

Tokens are verified locally against SUPABASE_JWT_SECRET — no GoTrue round-trip
per request. The cloud MCP server keeps its own SupabaseTokenVerifier; same
tokens, different transport.

Request-scoped dependencies fork on `get_config().api.auth`:

    request_tenancy(project, kb, request)          ──► (ctx, store), create=False
    request_tenancy_creating(project, kb, request)  ──► (ctx, store), create=True (explore only)
    request_store(request)                          ──► store, no project/kb binding

Each delegates untouched to the `api.auth == "none"` local helpers
(`resolve_kb_or_404` / `resolve_store`) and only takes the verify → require_beta
→ resolve_*_for_token path when `api.auth == "supabase"`.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from delapan.api.deps import resolve_kb_or_404
from delapan.core.agent.state import TenantContext
from delapan.core.config import get_config, get_settings
from delapan.store import Store, get_store


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
