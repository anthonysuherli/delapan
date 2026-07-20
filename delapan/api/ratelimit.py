"""Request rate limiting — slowapi, keyed by JWT subject (fallback: client IP).

    request ──► _key: jwt sub (unverified decode — identity for bucketing only)
                    │ absent/invalid ──► client IP
                    ▼
            Limiter(default = config api.rate_limit_default)

``slowapi`` (and ``pyjwt``, used by ``_key``) ship in the ``[cloud]`` extra. A
local-only install (``pip install delapan[local]``) must still import this
module cleanly — ``routes_explore.py`` always imports it — so the slowapi
import is guarded; absent it, `limiter` degrades to a no-op whose `.limit(...)`
decorator is the identity function. Generous config defaults (120/minute,
12/hour) mean the local tier never meaningfully hits the buckets either way.
"""

from __future__ import annotations

from delapan.core.config import get_config

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    _HAVE_SLOWAPI = True
except ImportError:  # local-only install, no [cloud] extra
    _HAVE_SLOWAPI = False


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
