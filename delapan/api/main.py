"""FastAPI app for the open-core engine — the loopback HTTP surface.

    uvicorn delapan.api.main:app ──► GET  /health
                                     GET  /api/projects
                                     ...  /api/projects/{p}/kbs/{k}/graph[/...]   (KG read/write)
                                     ...  /api/projects/{p}/kbs/{k}/findings[...] (list/get/delete)
                                     GET  /api/projects/{p}/kbs/{k}/synopsis|resume
                                     POST /api/projects/{p}/kbs/{k}/explore       (SSE)
                                     POST /api/projects/{p}/kbs/{k}/canvas/search (SSE)
                                     POST /api/projects/{p}/kbs/{k}/canvas/keep

The local mirror of the MCP surface plus KG read/write routes for the
knowledge-graph control panel (a browser frontend on :5173 — hence the CORS
allowance). Binds loopback only; the cloud tier's full HTTP surface (/agent,
/v1/*, /internal/*) stays behind the ``[cloud]`` extra.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from delapan.api.health import router as health_router
from delapan.api.ratelimit import enforce_default_limit, limiter
from delapan.api.routes_canvas import router as canvas_router
from delapan.api.routes_explore import router as explore_router
from delapan.api.routes_findings import router as findings_router
from delapan.api.routes_kg import router as kg_router
from delapan.api.routes_projects import router as projects_router
from delapan.core.config import get_settings

# The KG control panel's dev origins, always allowed alongside CORS_ORIGINS.
_FRONTEND_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

app = FastAPI(title="delapan (open-core)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted({*get_settings().cors_origins, *_FRONTEND_ORIGINS}),
    allow_methods=["*"],
    allow_headers=["*"],
)

try:  # slowapi ships in the [cloud] extra — local-only installs no-op instead
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # No SlowAPIMiddleware here: on this FastAPI/slowapi pairing it enforces
    # nothing (`include_router` wraps child routes in a `_IncludedRouter` with
    # no `.endpoint`, so slowapi's `app.routes` walk never finds a handler and
    # treats every route as exempt). `enforce_default_limit` below is the
    # replacement — see its docstring in ratelimit.py for the full chain.
    # ImportError is already logged once, in ratelimit.py's own guard.
except ImportError:
    pass

_API_DEP = Depends(enforce_default_limit)
app.include_router(health_router)
app.include_router(projects_router, dependencies=[_API_DEP])
app.include_router(kg_router, dependencies=[_API_DEP])
app.include_router(findings_router, dependencies=[_API_DEP])
app.include_router(explore_router, dependencies=[_API_DEP])
app.include_router(canvas_router, dependencies=[_API_DEP])


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
