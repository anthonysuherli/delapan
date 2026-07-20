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

from fastapi import Request

from delapan.core.config import get_config, get_settings

logger = logging.getLogger(__name__)

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    _HAVE_SLOWAPI = True
except ImportError:  # local-only install, no [cloud] extra
    _HAVE_SLOWAPI = False
    logger.warning(
        "slowapi not installed — rate limiting is DISABLED (install the [cloud] extra to enable it)"
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


def enforce_default_limit(request: Request) -> None:
    """FastAPI dependency: applies ``api.rate_limit_default`` to any route that
    has no ``@limiter.limit(...)`` decorator of its own.

    ``SlowAPIMiddleware`` was the documented mechanism for this, but it can't
    do the job on this FastAPI version: 0.139's ``include_router`` wraps child
    routes in a lazy ``_IncludedRouter`` (``fastapi/routing.py``) that has no
    ``.endpoint`` attribute and never flattens into ``app.routes``. slowapi
    0.1.10's ``_find_route_handler`` (``slowapi/middleware.py``) requires
    ``hasattr(route, "endpoint")`` and returns ``None`` for every route this
    app registers via ``include_router`` — i.e. all of them — so
    ``_should_exempt`` treats every request as exempt and the middleware
    enforces nothing. Confirmed against both installed sources; see
    ``main.py``'s comment where the middleware registration was removed.

    This dependency sidesteps ``app.routes`` entirely: it reads the matched
    endpoint off ``request.scope["endpoint"]``, which Starlette's ``Router.app``
    sets via ``scope.update(child_scope)`` (``starlette/routing.py``) during
    dispatch, before any dependency runs — regardless of how the route was
    registered. It then mirrors ``middleware.py``'s own
    ``_should_exempt``/``_check_limits`` pairing: skip routes already carrying
    a ``@limiter.limit(...)`` decorator (their own ``override_defaults=False``
    already folds the default in — running both here too would double-hit the
    same bucket for the same request), otherwise call the same internal entry
    point the decorator uses, ``Limiter._check_request_limit(request,
    endpoint_func, in_middleware)`` — ``in_middleware=True`` to match
    ``middleware.py``'s own call shape exactly.

    A no-op when slowapi isn't installed, so local-only installs (no
    ``[cloud]`` extra) can still depend on this unconditionally.
    """
    if not _HAVE_SLOWAPI:
        return
    handler = request.scope.get("endpoint")
    if handler is None:
        return
    name = f"{handler.__module__}.{handler.__name__}"
    if (
        name in limiter._exempt_routes
        or name in limiter._route_limits
        or name in limiter._dynamic_route_limits
    ):
        return
    limiter._check_request_limit(request, handler, True)
