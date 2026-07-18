"""Project/KB discovery + lifecycle routes — the HTTP mirror of the MCP tools.

    GET   /api/projects                     ──► store.list_projects()
    PATCH /api/projects/{project}           ──► store.set_archived(project)
    PATCH /api/projects/{project}/kbs/{kb}  ──► store.set_archived(project, kb)

The control panel's home screen reads the GET to populate its project/KB picker;
the row shape is whatever `Store.list_projects` returns, passed through.
Archiving is reversible and non-destructive — it stamps `archived_at` and
nothing else.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from delapan.mcp.tenancy import resolve_store

router = APIRouter(prefix="/api")


class ArchiveRequest(BaseModel):
    archived: bool


@router.get("/projects")
def list_projects(include_archived: bool = False) -> dict:
    return {"projects": resolve_store().list_projects(include_archived=include_archived)}


def _resolve_ids(store, project: str, kb: str | None) -> tuple[str, str | None]:
    """Names → ids, without creating anything. Raises 404 when absent."""
    try:
        org_id, project_id = store.resolve_project(project, create=False)
        kb_id = store.resolve_kb(org_id, project_id, kb, create=False) if kb else None
    except Exception as exc:  # noqa: BLE001 — missing project/KB is a 404, not a 500
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return project_id, kb_id


@router.patch("/projects/{project}")
def archive_project(project: str, body: ArchiveRequest) -> dict:
    store = resolve_store()
    project_id, _ = _resolve_ids(store, project, None)
    return store.set_archived(project_id=project_id, archived=body.archived)


@router.patch("/projects/{project}/kbs/{kb}")
def archive_kb(project: str, kb: str, body: ArchiveRequest) -> dict:
    store = resolve_store()
    project_id, kb_id = _resolve_ids(store, project, kb)
    return store.set_archived(project_id=project_id, kb_id=kb_id, archived=body.archived)
