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
