"""Project/KB discovery route — the HTTP mirror of `delapan_projects`.

    GET /api/projects ──► resolve_store() ──► {"projects": store.list_projects()}

The control panel's home screen reads this to populate its project/KB picker;
the row shape is whatever `Store.list_projects` returns, passed through.
"""

from __future__ import annotations

from fastapi import APIRouter

from delapan.mcp.tenancy import resolve_store

router = APIRouter(prefix="/api")


@router.get("/projects")
def list_projects() -> dict:
    return {"projects": resolve_store().list_projects()}
