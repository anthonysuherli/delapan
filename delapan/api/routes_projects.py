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
    """Names → ids, without creating anything. Raises 404 when absent.

    Only ``RuntimeError`` means "not found" — the Store contract promises that
    and nothing else. Catching broader would report a DB failure or a genuine
    bug as a 404 and hide it.
    """
    try:
        org_id, project_id = store.resolve_project(project, create=False)
        kb_id = store.resolve_kb(org_id, project_id, kb, create=False) if kb else None
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return project_id, kb_id


def _archive(store, project_id: str, kb_id: str | None, archived: bool) -> dict:
    """Apply the flag. ``set_archived`` raises RuntimeError for a missing target
    or a (project, kb) pair that doesn't belong together — both are 404s."""
    try:
        return store.set_archived(
            project_id=project_id, kb_id=kb_id, archived=archived
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/projects/{project}")
def archive_project(project: str, body: ArchiveRequest) -> dict:
    store = resolve_store()
    project_id, _ = _resolve_ids(store, project, None)
    return _archive(store, project_id, None, body.archived)


@router.patch("/projects/{project}/kbs/{kb}")
def archive_kb(project: str, kb: str, body: ArchiveRequest) -> dict:
    store = resolve_store()
    project_id, kb_id = _resolve_ids(store, project, kb)
    return _archive(store, project_id, kb_id, body.archived)
