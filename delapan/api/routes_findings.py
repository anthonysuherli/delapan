"""Findings / synopsis / resume routes — the KB read surface plus finding delete.

    GET    /api/projects/{p}/kbs/{k}/findings        ──► list_findings (category/limit) → {count,total,findings}
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

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from delapan.api.auth import request_tenancy
from delapan.core.agent.preamble import select_preamble
from delapan.core.agent.state import TenantContext
from delapan.core.config import get_config, get_settings
from delapan.core.curation.backlog import rank_backlog
from delapan.store import Store

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}")


@router.get("/findings")
def list_findings(
    category: str | None = None,
    limit: int = 50,
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy),
) -> dict:
    ctx, store = tenancy
    return store.list_findings(ctx.kb_id, category=category or None, limit=limit)


@router.get("/findings/{finding_id}")
def get_finding(
    finding_id: str, tenancy: tuple[TenantContext, Store] = Depends(request_tenancy)
) -> dict:
    ctx, store = tenancy
    try:
        return store.get_finding(ctx.kb_id, finding_id)
    except Exception:  # noqa: BLE001, S110 — not in this KB; try a cross-KB resolve below
        pass
    # A node/edge may cite a finding owned by another KB in the same org — e.g. a
    # unified graph merged from source KBs keeps its `grounded_in` ids, but the
    # findings stay in the source KBs. `findings.id` is globally unique, so fall
    # back to a global-by-id lookup before reporting the evidence as missing.
    try:
        return store.get_finding_global(finding_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"finding not found: {finding_id}") from exc


@router.delete("/findings/{finding_id}")
def delete_finding(
    finding_id: str, tenancy: tuple[TenantContext, Store] = Depends(request_tenancy)
) -> dict:
    ctx, store = tenancy
    try:
        store.get_finding(ctx.kb_id, finding_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"finding not found: {finding_id}") from exc
    store.delete_finding(ctx.kb_id, finding_id)
    return {"deleted": True}


@router.get("/synopsis")
def get_synopsis(tenancy: tuple[TenantContext, Store] = Depends(request_tenancy)) -> dict | None:
    ctx, store = tenancy
    return store.load_synopsis(ctx.kb_id)


@router.get("/resume")
async def resume(
    query: str | None = None,
    depth: Literal["shallow", "normal", "deep"] = "normal",
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy),
) -> JSONResponse:
    ctx, store = tenancy
    if query and not get_settings().openai_api_key:
        return JSONResponse(status_code=503, content={"error": "embeddings unavailable"})
    preamble, coverage = await select_preamble(
        query or None, store=store, kb_id=ctx.kb_id, depth=depth,
        surface="resume", org_id=ctx.org_id,
    )
    return JSONResponse({"preamble": preamble, "coverage": coverage})


@router.get("/backlog")
async def backlog(
    limit: int | None = None, tenancy: tuple[TenantContext, Store] = Depends(request_tenancy)
) -> JSONResponse:
    ctx, store = tenancy
    cfg = get_config().curation
    rows = await store.list_curation_topics(ctx.kb_id, limit=500)
    ranked = rank_backlog(rows or [], cfg, datetime.now(UTC))
    return JSONResponse({"topics": ranked[: (limit or cfg.backlog_limit)]})
