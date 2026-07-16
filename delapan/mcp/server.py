"""FastMCP stdio server — the open-core plugin's entry path into delapan.

    MCP tool call ──► resolve_tenant(project, kb) ──► get_store() ──► engine

A third entry path alongside the (cloud-only) HTTP API; it drains the same engine
through the Store seam, so one engine serves both tiers. The surface is
deliberately small — five tools:

    delapan_resume    — inject KB context (banner + preamble + coverage)
    delapan_search    — semantic search over existing findings
    delapan_explore   — run the research pipeline + persist findings
    delapan_backlog   — ranked gap/sparse queries awaiting research
    delapan_projects  — list the caller's projects/KBs

Run with: ``python -m delapan.mcp.server``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from delapan.core.agent.preamble import Depth, assess_coverage, band_findings, select_preamble
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.clients.embeddings import embed_text
from delapan.core.config import get_config, get_settings
from delapan.core.curation.backlog import rank_backlog
from delapan.core.curation.recorder import schedule_record
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
    preamble, coverage = await select_preamble(
        query, store=store, kb_id=ctx.kb_id, depth=depth, surface="resume", org_id=ctx.org_id
    )
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

    cur = get_config().curation
    tiers = get_config().tiers
    limit_used = limit or 10
    # A `rich` verdict needs `rich_hit_count` band-1 hits; below that the verdict
    # would be an artifact of the caller's limit, not of the KB.
    if cur.record_search and limit_used >= tiers.rich_hit_count:
        bands = band_findings(hits or [], tiers)
        schedule_record(
            store,
            kb_id=ctx.kb_id,
            org_id=ctx.org_id,
            surface="search",
            query=query,
            coverage=assess_coverage(bands, tiers),
            bands=bands,
            embedding=emb,
        )
    return {"query": query, "findings": hits}


# --- Curation backlog --------------------------------------------------------


@mcp.tool()
async def delapan_backlog(project: str, kb: str, limit: int | None = None) -> dict:
    """The KB's curation backlog — gap/sparse queries it was asked and could not
    answer, ranked by recurrence × severity × recency. Returns ``{"topics": [...]}``,
    each with ``id, query_text, coverage, recurrence, last_seen, score``. Feed the
    top one to ``delapan_explore`` (or call explore with no prompt to consume it)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().curation
    rows = await store.list_curation_topics(ctx.kb_id, limit=limit or cfg.backlog_limit)
    ranked = rank_backlog(rows or [], cfg, datetime.now(timezone.utc))
    return {"topics": ranked[: (limit or cfg.backlog_limit)]}


# --- Build the KB ----------------------------------------------------------


@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str | None = None, max_findings: int | None = None
) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating the project/KB on demand). Blocks until
    complete (may take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "finding_ids", "count"}``.

    With no ``prompt``, consumes the top item of the KB's curation backlog — the
    gap the KB was asked about most — and adds ``"backlog_topic"`` to the result.
    An empty backlog returns an error and creates nothing."""
    topic_id: str | None = None
    if prompt is None:
        try:
            ctx = resolve_tenant(project, kb, create=False)
        except Exception as exc:  # noqa: BLE001 — never create a KB to read a backlog
            return {"error": f"KB not found ({project}/{kb}): {exc}"}
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        cur = get_config().curation
        rows = await store.list_curation_topics(ctx.kb_id, limit=cur.backlog_limit)
        ranked = rank_backlog(rows or [], cur, datetime.now(timezone.utc))
        if not ranked:
            return {
                "error": "backlog empty — pass a prompt, or run resume/search so gaps get recorded"
            }
        top = ranked[0]
        topic_id, prompt = top["id"], top["query_text"]
        await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=_now_iso())
    else:
        ctx = resolve_tenant(project, kb, create=True)
        store = get_store(ctx.access_token, org_id=ctx.org_id)

    cfg = get_config().exploration
    cap = min(max_findings or cfg.default_max_findings, cfg.max_findings)

    exp_id: str | None = None
    try:
        exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, prompt)
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
    except Exception as exc:  # noqa: BLE001 — restore the topic, then re-raise the original
        if topic_id:  # a failed run must return the topic to the backlog
            try:
                await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=None)
            except Exception:  # noqa: BLE001 — best-effort; the raise below is the signal
                pass
        if exp_id is not None:  # no row to mark failed if create_exploration itself failed
            try:
                store.update_exploration(
                    exp_id, status="failed", completed_at=_now_iso(), error=str(exc)
                )
            except Exception:  # noqa: BLE001 — bookkeeping must never mask the original exc
                pass
        raise

    out = {"exploration_id": exp_id, "finding_ids": ids, "count": len(ids)}
    if topic_id:
        out["backlog_topic"] = topic_id
    return out


# --- Tenancy ---------------------------------------------------------------


@mcp.tool()
async def delapan_projects() -> dict:
    """List the caller's projects (by name) with their KBs — for client discovery.
    Returns ``{"projects": [...]}``."""
    store = resolve_store()
    return {"projects": store.list_projects()}


def main() -> None:
    get_settings()  # fail fast if infra env is missing
    mcp.run()


if __name__ == "__main__":
    main()
