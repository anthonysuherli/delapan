"""FastMCP stdio server — the open-core plugin's entry path into delapan.

    MCP tool call ──► resolve_tenant(project, kb) ──► get_store() ──► engine

A third entry path alongside the (cloud-only) HTTP API; it drains the same engine
through the Store seam, so one engine serves both tiers. The surface is
deliberately small — four tools:

    delapan_resume    — inject KB context (banner + preamble + coverage)
    delapan_search    — semantic search over existing findings
    delapan_explore   — run the research pipeline + persist findings
    delapan_projects  — list the caller's projects/KBs

Each tool is a thin tenancy-resolution wrapper around a shared ``_*_impl``
function; ``delapan/mcp/cloud_server.py`` reuses those same ``_*_impl``
functions with claude.ai-token-based tenancy resolution instead of this
module's fixed-configured-user login, so the two entrypoints share one copy
of the actual tool logic.

Run with: ``python -m delapan.mcp.server``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from delapan.core.agent.preamble import Depth, select_preamble
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.agent.state import TenantContext
from delapan.core.clients.embeddings import embed_text
from delapan.core.config import get_config, get_settings
from delapan.core.exploration import run_exploration
from delapan.core.knowledge_graph.builder import schedule_kg_update
from delapan.core.memory.persist import resolve_and_persist
from delapan.store import get_store

from .banner import DELAPAN_BANNER
from .tenancy import resolve_store, resolve_tenant

logger = logging.getLogger(__name__)

mcp = FastMCP("delapan")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Inject → this conversation --------------------------------------------


async def _resume_impl(ctx: TenantContext, query: str | None, depth: Depth) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    preamble, coverage = await select_preamble(query, store=store, kb_id=ctx.kb_id, depth=depth)
    return {"banner": DELAPAN_BANNER, "preamble": preamble, "coverage": coverage}


@mcp.tool()
async def delapan_resume(
    project: str, kb: str, query: str | None = None, depth: Depth = "normal"
) -> dict:
    """Inject KB context into THIS conversation. Returns ``{"banner", "preamble",
    "coverage"}``: the delapan wordmark to lead the message with, the <preamble>
    (synopsis spine plus query-relevant findings), and the coverage band for the
    query (``rich``/``sparse``/``gap``). The same rendering + signal the cloud
    ``/v1/preamble`` serves to apps (both go through ``select_preamble``)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _resume_impl(ctx, query, depth)


# --- Recall ----------------------------------------------------------------


async def _search_impl(ctx: TenantContext, query: str, limit: int | None) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    emb = await embed_text(query)
    hits = await store.match_findings(ctx.kb_id, emb, match_count=limit or 10, min_similarity=0.0)
    return {"query": query, "findings": hits}


@mcp.tool()
async def delapan_search(project: str, kb: str, query: str, limit: int | None = None) -> dict:
    """Recall from the KB only — semantic search over existing findings, no web.
    Returns ``{"query", "findings"}`` with the ranked finding rows (each carries a
    ``similarity``)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _search_impl(ctx, query, limit)


# --- Build the KB ------------------------------------------------------------


async def _explore_impl(ctx: TenantContext, prompt: str, max_findings: int | None) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().exploration
    cap = min(max_findings or cfg.default_max_findings, cfg.max_findings)

    exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, prompt)
    try:
        findings = await run_exploration(
            prompt,
            exploration_id=exp_id,
            project_id=ctx.project_id,
            kb_id=ctx.kb_id,
            cfg=cfg,
        )
        captured = findings[:cap]

        outcome = await resolve_and_persist(ctx, store, captured, get_config())
        ids = outcome.affected_finding_ids

        store.update_exploration(
            exp_id, status="completed", completed_at=_now_iso(), finding_ids=ids
        )
        # Grow the stable layers from the new findings. Synopsis rebuild is awaited
        # (best-effort, never raises); the KG update is fire-and-forget (sync
        # scheduler, gated on an approved intent schema — no-op otherwise).
        syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise

    return {
        "exploration_id": exp_id,
        "finding_ids": ids,
        "count": len(ids),
        "synopsis": syn_status,
    }


@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str, max_findings: int | None = None
) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating the project/KB on demand). Blocks until
    complete (may take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "finding_ids", "count", "synopsis"}`` — ``synopsis`` is the
    rebuild status (``"rebuilt"``/``"skipped"``/``"failed: <msg>"``)."""
    ctx = resolve_tenant(project, kb, create=True)
    return await _explore_impl(ctx, prompt, max_findings)


# --- Tenancy ---------------------------------------------------------------


def _projects_impl(store) -> dict:
    return {"projects": store.list_projects()}


@mcp.tool()
async def delapan_projects() -> dict:
    """List the caller's projects (by name) with their KBs — for client discovery.
    Returns ``{"projects": [...]}``."""
    store = resolve_store()
    return _projects_impl(store)


def main() -> None:
    get_settings()  # fail fast if infra env is missing
    mcp.run()


if __name__ == "__main__":
    main()
