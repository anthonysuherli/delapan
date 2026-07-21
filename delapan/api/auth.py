"""Bearer auth + beta gating for the public /api surface.

    Authorization: Bearer <supabase JWT>
        │ api.auth == "none"     ──► resolve_kb_or_404 / resolve_store (unchanged)
        └ api.auth == "supabase" ──► verify_bearer
                                       │ supabase_url set   ──► ES256 via JWKS (PyJWKClient)
                                       │   known kid, bad sig/exp/aud  ──► 401 (no fallback)
                                       │   unknown/absent kid          ──► try HS256 below
                                       │   JWKS endpoint unreachable   ──► 503
                                       └ SUPABASE_JWT_SECRET set ──► HS256 shared secret (legacy)
                                       ──► require_beta (beta_members via service client)
                                       ──► resolve_*_for_token ──► (ctx, org-scoped store)

Tokens are verified locally — no GoTrue round-trip per request. Production
Supabase projects sign with asymmetric ES256 keys, so `verify_bearer` fetches
the project's public JWKS (`PyJWKClient`, cached, module-level seam
`_jwk_client`) and verifies against it first. Self-hosted/legacy projects that
still mint HS256 tokens against a shared `SUPABASE_JWT_SECRET` keep working as
a fallback — but it's only consulted when ES256 doesn't apply (no
`supabase_url`, or the token's `kid` isn't in the JWKS): a token whose `kid`
*does* match a known ES256 key but fails verification (bad signature,
expired, wrong audience) is rejected outright, never silently retried against
the shared secret. A JWKS fetch that fails on the network is reported as a
503, not a 401 — an outage must not read as a forged token. The cloud MCP
server keeps its own SupabaseTokenVerifier; same tokens, different transport.

Request-scoped dependencies fork on `get_config().api.auth`:

    request_tenancy(project, kb, request)          ──► (ctx, store), create=False
    request_tenancy_creating(project, kb, request)  ──► (ctx, store), create=True (explore only)
    request_store(request)                          ──► store, no project/kb binding

Each delegates untouched to the `api.auth == "none"` local helpers
(`resolve_kb_or_404` / `resolve_store`) and only takes the verify → require_beta
→ resolve_*_for_token path when `api.auth == "supabase"`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from delapan.api.deps import resolve_kb_or_404
from delapan.core.agent.state import TenantContext
from delapan.core.config import get_config, get_settings
from delapan.store import Store, get_store

if TYPE_CHECKING:
    import jwt  # pyjwt — [cloud] extra; import deferred at runtime (see below)

_jwk_client_cache: jwt.PyJWKClient | None = None


def _jwk_client() -> jwt.PyJWKClient:
    """Cached PyJWKClient for the project's JWKS endpoint — built lazily, once,
    never per request (it keeps its own TTL cache over the fetched keys).
    Module-level seam for test fakes, mirroring `_service_client()` below."""
    import jwt  # pyjwt — [cloud] extra

    global _jwk_client_cache
    if _jwk_client_cache is None:
        url = get_settings().supabase_url
        _jwk_client_cache = jwt.PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json")
    return _jwk_client_cache


def _verify_es256(token: str) -> dict[str, Any] | None:
    """Verify against the project's JWKS. Returns ``None`` when the token
    isn't a JWKS match (no/unknown `kid`, or an unparsable header) so the
    caller can fall back to HS256. Raises 401 for a token whose `kid` *does*
    match a known key but fails verification, and 503 if the JWKS endpoint
    itself can't be reached — a network outage must not read as a forged
    token."""
    import jwt  # pyjwt — [cloud] extra

    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(status_code=503, detail="JWKS endpoint unreachable") from exc
    except jwt.PyJWTError:
        return None
    try:
        return jwt.decode(token, signing_key.key, algorithms=["ES256"], audience="authenticated")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc


def _verify_hs256(token: str, secret: str) -> dict[str, Any]:
    import jwt  # pyjwt — [cloud] extra

    try:
        return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc


def verify_bearer(authorization: str | None) -> str:
    """Validate an `Authorization: Bearer <jwt>` header; return the Supabase user id.

    ES256 via JWKS is tried first when `supabase_url` is configured; HS256
    against `SUPABASE_JWT_SECRET` is the legacy fallback, consulted only when
    ES256 doesn't apply (see `_verify_es256`'s docstring for exactly when)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ")
    settings = get_settings()

    claims = _verify_es256(token) if settings.supabase_url else None
    if claims is None:
        if settings.supabase_jwt_secret:
            claims = _verify_hs256(token, settings.supabase_jwt_secret)
        elif settings.supabase_url:
            raise HTTPException(status_code=401, detail="invalid or expired token")
        else:
            raise HTTPException(
                status_code=500, detail="SUPABASE_URL or SUPABASE_JWT_SECRET not configured"
            )
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
    if get_config().api.auth == "supabase":
        return _authed_tenancy(project, kb, request, create=False)
    return resolve_kb_or_404(project, kb)


def request_tenancy_creating(project: str, kb: str, request: Request) -> tuple[TenantContext, Store]:
    """Explore-only variant: the cloud path may create the project/KB on demand
    (mirrors MCP explore; the dashboard's first-run explore needs it). Local
    HTTP stays non-creating."""
    if get_config().api.auth == "supabase":
        return _authed_tenancy(project, kb, request, create=True)
    return resolve_kb_or_404(project, kb)


def request_store(request: Request) -> Store:
    """Org-scoped store with no project/kb binding (projects/discovery routes)."""
    if get_config().api.auth == "supabase":
        from delapan.mcp.tenancy import resolve_store_for_token

        authorization = request.headers.get("authorization")
        user_id = verify_bearer(authorization)
        require_beta(user_id)
        return resolve_store_for_token(user_id, authorization.removeprefix("Bearer "))
    from delapan.mcp.tenancy import resolve_store

    return resolve_store()
