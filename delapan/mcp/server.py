"""FastMCP stdio server — the open-core plugin's entry path into delapan.

    MCP tool call ──► resolve_tenant(project, kb) ──► get_store() ──► engine

A third entry path alongside the (cloud-only) HTTP API; it drains the same engine
through the Store seam, so one engine serves both tiers. The surface is
deliberately small — five tools:

    delapan_resume    — inject KB context (banner + preamble + coverage)
    delapan_search    — semantic search over existing findings
    delapan_explore   — run the research pipeline + persist findings
    delapan_projects  — list the caller's projects/KBs
    delapan_archive   — archive/unarchive a project or KB (reversible)

Run with: ``python -m delapan.mcp.server``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from delapan.core.agent.preamble import Depth, select_preamble
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
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
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    preamble, coverage = await select_preamble(query, store=store, kb_id=ctx.kb_id, depth=depth)
    return {"banner": DELAPAN_BANNER, "preamble": preamble, "coverage": coverage}


# --- Recall ----------------------------------------------------------------


@mcp.tool()
async def delapan_search(project: str, kb: str, query: str, limit: int | None = None) -> dict:
    """Recall from the KB only — semantic search over existing findings, no web.
    Returns ``{"query", "findings"}`` with the ranked finding rows (each carries a
    ``similarity``)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    emb = await embed_text(query)
    hits = await store.match_findings(ctx.kb_id, emb, match_count=limit or 10, min_similarity=0.0)
    return {"query": query, "findings": hits}


# --- Build the KB ----------------------------------------------------------


def _clear_archive(store, ctx) -> bool:
    """Unarchive ctx's KB and project if either was archived. Returns whether
    anything changed — explore reports it so the flip is visible, not silent."""
    project = next(
        (
            p
            for p in store.list_projects(include_archived=True)
            if p["project_id"] == ctx.project_id
        ),
        None,
    )
    if project is None:
        return False
    kb = next((k for k in project["kbs"] if k["kb_id"] == ctx.kb_id), None)
    was_archived = project["archived_at"] is not None or (
        kb is not None and kb["archived_at"] is not None
    )
    if was_archived:
        store.set_archived(project_id=ctx.project_id, kb_id=ctx.kb_id, archived=False)
        store.set_archived(project_id=ctx.project_id, archived=False)
    return was_archived


@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str, max_findings: int | None = None
) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating the project/KB on demand). Blocks until
    complete (may take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "finding_ids", "count", "unarchived"}``."""
    ctx = resolve_tenant(project, kb, create=True)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    # Writing to a KB means it's live again. Either flag hides it, so clear both
    # the KB's and its project's, and report the flip in the result so the state
    # change is never silent.
    was_archived = _clear_archive(store, ctx)
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
        await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise

    return {
        "exploration_id": exp_id,
        "finding_ids": ids,
        "count": len(ids),
        "unarchived": was_archived,
    }


# --- Tenancy ---------------------------------------------------------------


@mcp.tool()
async def delapan_projects(include_archived: bool = False) -> dict:
    """List the caller's projects (by name) with their KBs — for client discovery.
    Each KB carries ``finding_count`` and ``last_finding_at`` (live findings only).
    Archived projects/KBs are omitted unless ``include_archived`` is true.
    Returns ``{"projects": [...]}``."""
    store = resolve_store()
    return {"projects": store.list_projects(include_archived=include_archived)}


@mcp.tool()
async def delapan_archive(project: str, kb: str | None = None, archived: bool = True) -> dict:
    """Archive or unarchive a project (omit ``kb``) or a single KB. Reversible and
    non-destructive — stamps ``archived_at`` and touches no finding, node, or edge.
    Archived KBs drop out of ``delapan_projects`` but stay fully readable by
    ``delapan_resume`` / ``delapan_search``; running ``delapan_explore`` against one
    unarchives it. Returns ``{"project", "kb", "archived", "archived_at",
    "finding_count"}`` — check ``finding_count`` to see what you just put away."""
    store = resolve_store()
    try:
        org_id, project_id = store.resolve_project(project, create=False)
        kb_id = store.resolve_kb(org_id, project_id, kb, create=False) if kb else None
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        target = f"{project}/{kb}" if kb else project
        return {"error": f"Not found ({target}): {exc}"}

    out = store.set_archived(project_id=project_id, kb_id=kb_id, archived=archived)
    return {
        "project": project,
        "kb": kb,
        "archived": archived,
        "archived_at": out["archived_at"],
        "finding_count": out["finding_count"],
    }


def main() -> None:
    get_settings()  # fail fast if infra env is missing
    mcp.run()


if __name__ == "__main__":
    main()
