"""Findings / synopsis / resume routes — the KB read surface plus finding delete.

    GET    /api/projects/{p}/kbs/{k}/findings        ──► list_findings (category/limit)
    GET    /api/projects/{p}/kbs/{k}/findings/{id}   ──► get_finding (content+provenance)*
    DELETE /api/projects/{p}/kbs/{k}/findings/{id}   ──► delete_finding
    GET    /api/projects/{p}/kbs/{k}/synopsis        ──► load_synopsis (row or null)
    GET    /api/projects/{p}/kbs/{k}/resume          ──► select_preamble → {preamble, coverage}

*`get_finding` tries the scoped KB first, then falls back to a global-by-id
lookup so cross-KB ``grounded_in`` citations resolve — e.g. a unified graph whose
nodes/edges cite findings owned by the source KBs they were merged from.

`resume` needs a query embedding (OPENAI_API_KEY); with a query but no key it
returns 503 ``{"error": "embeddings unavailable"}`` instead of crashing. With no
query the preamble is synopsis-only and works keyless (coverage = "gap").
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from delapan.api.deps import resolve_kb_or_404
from delapan.core.agent.preamble import select_preamble
from delapan.core.config import get_settings

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}")


@router.get("/findings")
def list_findings(project: str, kb: str, category: str | None = None, limit: int = 50) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    return store.list_findings(ctx.kb_id, category=category or None, limit=limit)


@router.get("/findings/{finding_id}")
def get_finding(project: str, kb: str, finding_id: str) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    try:
        return store.get_finding(ctx.kb_id, finding_id)
    except Exception:  # noqa: BLE001 — not in this KB; try a cross-KB resolve below
        pass
    # A node/edge may cite a finding owned by another KB in the same org — e.g. a
    # unified graph merged from source KBs keeps its `grounded_in` ids, but the
    # findings stay in the source KBs. `findings.id` is globally unique, so fall
    # back to a global-by-id lookup before reporting the evidence as missing.
    try:
        return store.get_finding_global(finding_id)
    except Exception as exc:  # noqa: BLE001 — store raises on a missing finding
        raise HTTPException(status_code=404, detail=f"finding not found: {finding_id}") from exc


@router.delete("/findings/{finding_id}")
def delete_finding(project: str, kb: str, finding_id: str) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    try:
        store.get_finding(ctx.kb_id, finding_id)
    except Exception as exc:  # noqa: BLE001 — store raises on a missing finding
        raise HTTPException(status_code=404, detail=f"finding not found: {finding_id}") from exc
    store.delete_finding(ctx.kb_id, finding_id)
    return {"deleted": True}


@router.get("/synopsis")
def get_synopsis(project: str, kb: str) -> dict | None:
    ctx, store = resolve_kb_or_404(project, kb)
    return store.load_synopsis(ctx.kb_id)


@router.get("/resume")
async def resume(
    project: str,
    kb: str,
    query: str | None = None,
    depth: Literal["shallow", "normal", "deep"] = "normal",
) -> JSONResponse:
    ctx, store = resolve_kb_or_404(project, kb)
    if query and not get_settings().openai_api_key:
        return JSONResponse(status_code=503, content={"error": "embeddings unavailable"})
    preamble, coverage = await select_preamble(
        query or None, store=store, kb_id=ctx.kb_id, depth=depth
    )
    return JSONResponse({"preamble": preamble, "coverage": coverage})
