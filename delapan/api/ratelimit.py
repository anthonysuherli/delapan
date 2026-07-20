"""Request rate limiting — slowapi, keyed by verified JWT subject (fallback: client IP).

    request ──► _key: jwt sub (verified decode, HS256/supabase_jwt_secret)
                    │ absent/invalid/unverifiable ──► client IP
                    ▼
            Limiter(default = config api.rate_limit_default)

``slowapi`` (and ``pyjwt``, used by ``_key``) ship in the ``[cloud]`` extra. A
local-only install (``pip install delapan[local]``) must still import this
module cleanly — ``routes_explore.py`` always imports it — so the slowapi
import is guarded; absent it, `limiter` degrades to a no-op whose `.limit(...)`
decorator is the identity function (logged as a warning — rate limiting is
DISABLED). Generous config defaults (120/minute, 12/hour) mean the local tier
never meaningfully hits the buckets either way.
"""

from __future__ import annotations

import logging

from delapan.core.config import get_config, get_settings

logger = logging.getLogger(__name__)

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    _HAVE_SLOWAPI = True
except ImportError:  # local-only install, no [cloud] extra
    _HAVE_SLOWAPI = False
    logger.warning(
        "slowapi not installed — rate limiting is DISABLED (install the [cloud] extra "
        "to enable it)"
    )


def _key(request) -> str:
    """Bucket by verified JWT subject; any decode failure falls back to client IP
    so a forged/garbage-signed token can never mint its own per-sub bucket."""
    auth = request.headers.get("authorization", "")
    secret = get_settings().supabase_jwt_secret
    if auth.startswith("Bearer ") and secret:
        import jwt

        try:
            claims = jwt.decode(
                auth.removeprefix("Bearer "),
                secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
            return claims.get("sub") or get_remote_address(request)
        except jwt.PyJWTError:
            pass
    return get_remote_address(request)


class _NoOpLimiter:
    """Fallback when slowapi isn't installed — `.limit(...)` is the identity decorator."""

    def limit(self, *_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


limiter = (
    Limiter(
        key_func=_key,
        default_limits=[lambda: get_config().api.rate_limit_default],
        headers_enabled=True,  # required for the 429 response to carry Retry-After
    )
    if _HAVE_SLOWAPI
    else _NoOpLimiter()
)


def pipeline_limit() -> str:
    return get_config().api.rate_limit_pipeline
