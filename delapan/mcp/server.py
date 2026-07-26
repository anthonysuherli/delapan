"""FastMCP stdio server — the open-core plugin's entry path into delapan.

    MCP tool call ──► resolve_tenant(project, kb) ──► get_store() ──► engine

A third entry path alongside the (cloud-only) HTTP API; it drains the same engine
through the Store seam, so one engine serves both tiers. The surface is
deliberately small — ten tools:

    delapan_resume            — inject KB context (banner + preamble + coverage)
    delapan_search            — semantic search over existing findings
    delapan_explore           — run the research pipeline + persist findings
    delapan_backlog           — ranked gap/sparse queries awaiting research
    delapan_projects          — list the caller's projects/KBs
    delapan_archive           — archive/unarchive a project or KB (reversible)
    delapan_propose_kg_schema — draft a target ontology from the KB's findings
    delapan_set_kg_schema     — validate + persist the approved ontology (versioned)
    delapan_get_kg_schema     — intent vs emergent ontology, side by side
    delapan_build_graph       — build/refresh the KG (schema-steered when set)

Each tool is a thin tenancy-resolution wrapper around a shared ``_*_impl``
function; ``delapan/mcp/cloud_server.py`` reuses those same ``_*_impl``
functions with claude.ai-token-based tenancy resolution instead of this
module's fixed-configured-user login, so the two entrypoints share one copy
of the actual tool logic.

Run with: ``python -m delapan.mcp.server``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from delapan.core.agent.preamble import Depth, assess_coverage, band_findings, select_preamble
from delapan.core.agent.state import TenantContext
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.clients.embeddings import MissingEmbeddingKeyError, embed_text
from delapan.core.config import get_config, get_settings, missing_pipeline_keys
from delapan.core.curation.backlog import rank_backlog
from delapan.core.curation.recorder import schedule_record
from delapan.core.exploration import run_exploration
from delapan.core.knowledge_graph.builder import _gather_findings, build_graph, schedule_kg_update
from delapan.core.knowledge_graph.schema import KGSchema, propose_schema, validate_schema
from delapan.core.knowledge_graph.service import kg_schema_view
from delapan.core.memory.persist import resolve_and_persist
from delapan.store import get_store

from .banner import DELAPAN_BANNER
from .onboarding import kb_not_found_card, seed_demo_if_absent
from .tenancy import resolve_store, resolve_tenant

logger = logging.getLogger(__name__)

mcp = FastMCP("delapan")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- Inject → this conversation --------------------------------------------


async def _resume_impl(ctx: TenantContext, query: str | None, depth: Depth) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    preamble, coverage = await select_preamble(
        query, store=store, kb_id=ctx.kb_id, depth=depth, surface="resume", org_id=ctx.org_id
    )
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
    except Exception as exc:  # noqa: BLE001 — onboarding card for a missing project/KB
        try:
            store = resolve_store()
        except Exception:  # noqa: BLE001 — the card must render even when the store won't
            store = None
        return kb_not_found_card(project, kb, exc, store=store)
    try:
        return await _resume_impl(ctx, query, depth)
    except MissingEmbeddingKeyError as exc:
        return {"error": str(exc)}


# --- Recall ----------------------------------------------------------------


async def _search_impl(ctx: TenantContext, query: str, limit: int | None) -> dict:
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


@mcp.tool()
async def delapan_search(project: str, kb: str, query: str, limit: int | None = None) -> dict:
    """Recall from the KB only — semantic search over existing findings, no web.
    Returns ``{"query", "findings"}`` with the ranked finding rows (each carries a
    ``similarity``)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    try:
        return await _search_impl(ctx, query, limit)
    except MissingEmbeddingKeyError as exc:
        return {"error": str(exc)}


# --- Build the KB ------------------------------------------------------------


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


async def _explore_impl(ctx: TenantContext, prompt: str | None, max_findings: int | None) -> dict:
    missing = missing_pipeline_keys()
    if missing:
        return {
            "error": "explore needs credentials: set "
            + " and ".join(missing)
            + " in the plugin root's .env (see .env.example) — resume works without them."
        }
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    # Promptless: consume the KB's top curation gap in place of a caller prompt.
    # Resolved before any archive flip so an empty backlog changes nothing.
    topic_id: str | None = None
    if prompt is None:
        cur = get_config().curation
        rows = await store.list_curation_topics(ctx.kb_id, limit=500)
        ranked = rank_backlog(rows or [], cur, datetime.now(UTC))
        if not ranked:
            return {
                "error": "backlog empty — pass a prompt, or run resume/search so gaps get recorded"
            }
        top = ranked[0]
        topic_id, prompt = top["id"], top["query_text"]
        await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=_now_iso())

    # Writing to a KB means it's live again. Either flag hides it, so clear both
    # the KB's and its project's, and report the flip in the result so the state
    # change is never silent.
    was_archived = _clear_archive(store, ctx)
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

        if not captured:
            store.update_exploration(
                exp_id, status="empty", completed_at=_now_iso(), finding_ids=[]
            )
            if topic_id:  # the gap was not filled — return the topic to the backlog
                await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=None)
            return {
                "exploration_id": exp_id,
                "status": "empty",
                "count": 0,
                "reason": (
                    "the pipeline produced no findings — search returned nothing "
                    "(check your Tavily quota) or every extracted finding fell below "
                    "the confidence floor; retry with a more specific prompt"
                ),
                "unarchived": was_archived,
            }

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
    except Exception as exc:
        if topic_id:  # a failed run must return the topic to the backlog
            try:
                await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=None)
            except Exception:  # noqa: BLE001, S110 — best-effort; the raise below is the signal
                pass
        if exp_id is not None:  # no row to mark failed if create_exploration itself failed
            try:
                store.update_exploration(
                    exp_id, status="failed", completed_at=_now_iso(), error=str(exc)
                )
            except Exception:  # noqa: BLE001, S110 — bookkeeping must never mask the original exc
                pass
        raise

    out = {
        "exploration_id": exp_id,
        "status": "completed",
        "finding_ids": ids,
        "count": len(ids),
        "synopsis": syn_status,
        "unarchived": was_archived,
    }
    if not ids:
        out["note"] = "all findings resolved as duplicates of existing knowledge (no new rows)"
    if topic_id:
        out["backlog_topic"] = topic_id
    return out


@mcp.tool()
async def delapan_explore(
    project: str, kb: str, prompt: str | None = None, max_findings: int | None = None
) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings to the named KB (creating the project/KB on demand). Blocks until
    complete (may take several minutes; the calling client may time out). Returns
    ``{"exploration_id", "status", "finding_ids", "count", "synopsis", "unarchived"}`` —
    ``status`` is ``"completed"`` or ``"empty"`` (the pipeline produced zero findings;
    an empty run also adds ``"reason"`` and returns any consumed backlog topic instead
    of a ``"finding_ids"``/``"synopsis"`` pair), ``synopsis`` is the rebuild status
    (``"rebuilt"``/``"skipped"``/``"failed: <msg>"``), ``unarchived`` reports whether
    writing here flipped an archived KB back to live. A ``"completed"`` run where every
    finding resolved as a duplicate adds ``"note"``.

    With no ``prompt``, consumes the top item of the KB's curation backlog — the
    gap the KB was asked about most — and adds ``"backlog_topic"`` to the result.
    An empty backlog returns an error and creates nothing."""
    try:  # a promptless call reads a backlog, so it must never create the KB
        ctx = resolve_tenant(project, kb, create=prompt is not None)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _explore_impl(ctx, prompt, max_findings)


# --- Curation backlog --------------------------------------------------------


@mcp.tool()
async def delapan_backlog(project: str, kb: str, limit: int | None = None) -> dict:
    """The KB's curation backlog — gap/sparse queries it was asked and could not
    answer, ranked by recurrence × severity × recency. Returns ``{"topics": [...]}``,
    each entry the full topic row (``id, query_text, query_norm, coverage,
    recurrence, first_seen, last_seen, consumed_at, resolved_at``, etc.) plus a
    computed ``score``. Feed the top one to ``delapan_explore`` (or call explore
    with no prompt to consume it)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().curation
    rows = await store.list_curation_topics(ctx.kb_id, limit=500)
    ranked = rank_backlog(rows or [], cfg, datetime.now(UTC))
    return {"topics": ranked[: (limit or cfg.backlog_limit)]}


# --- KG intent schema (co-design seam) ---------------------------------------


async def _propose_kg_schema_impl(ctx: TenantContext, max_findings: int | None) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().knowledge_graph
    # The Store's list view omits `content`, so reuse the builder's hydrating
    # loader — a titles-only catalogue would starve the proposer of grounding.
    findings = _gather_findings(
        store, ctx.kb_id, finding_ids=None, max_findings=max_findings or cfg.max_findings
    )
    stats = store.kg_stats(ctx.kb_id)
    # Bias the draft toward the ontology an already-built graph grew organically.
    emergent: dict | None = None
    if stats.get("node_count", 0) or stats.get("edge_count", 0):
        emergent = {
            "node_types": list((stats.get("by_type") or {}).keys()),
            "relations": list((stats.get("by_relation") or {}).keys()),
        }
    draft = await propose_schema(findings, cfg, emergent=emergent)
    out = draft.model_dump()
    if not findings:
        out["note"] = "KB has no findings — explore or ingest first for a grounded proposal."
    return out


@mcp.tool()
async def delapan_propose_kg_schema(project: str, kb: str, max_findings: int | None = None) -> dict:
    """STEP 1 of KG-intent co-design. Mine the KB's findings and propose a draft
    target ontology: ``node_types``, ``relation_types``, ``relation_validity``,
    and ``competency_questions``. Persists nothing — review with the user, then
    approve with ``delapan_set_kg_schema``. If the KB has no findings the draft
    is a generic default plus a ``note``."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _propose_kg_schema_impl(ctx, max_findings)


def _set_kg_schema_impl(ctx: TenantContext, schema: dict) -> dict:
    try:
        parsed = KGSchema.model_validate(schema)
    except Exception as exc:  # noqa: BLE001 — surface validation errors to the caller
        return {"ok": False, "errors": [f"schema does not parse: {exc}"]}
    errors = validate_schema(parsed)
    if errors:
        return {"ok": False, "errors": errors}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    stored = store.set_kg_intent(ctx.org_id, ctx.kb_id, parsed.model_dump())
    return {"ok": True, "schema": stored}


@mcp.tool()
async def delapan_set_kg_schema(project: str, kb: str, schema: dict) -> dict:
    """STEP 2 of KG-intent co-design. Validate and persist the user-approved KG
    schema dict (as returned by ``delapan_propose_kg_schema``, edited as the user
    wishes) as a new version. Returns ``{ok: true, schema}`` on success, or
    ``{ok: false, errors}`` when the schema is malformed (nothing is saved).
    The next ``delapan_build_graph(use_schema=True)`` builds against it."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return _set_kg_schema_impl(ctx, schema)


def _get_kg_schema_impl(ctx: TenantContext) -> dict:
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    return kg_schema_view(store, ctx.kb_id)


@mcp.tool()
async def delapan_get_kg_schema(project: str, kb: str) -> dict:
    """Both ontologies for the KB: ``intent`` (the approved target schema set via
    ``delapan_set_kg_schema``, or null) and ``emergent`` (the node/relation types
    actually present in the built graph). Compare the two to see drift — the same
    view the HTTP ``GET .../graph/schema`` serves."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return _get_kg_schema_impl(ctx)


@mcp.tool()
async def delapan_build_graph(
    project: str,
    kb: str,
    max_findings: int | None = None,
    rebuild: bool = True,
    use_schema: bool = True,
) -> dict:
    """Build/refresh the KB's knowledge graph from its findings. An LLM extracts
    entities + relationships, deduped into kg_nodes/kg_edges. ``rebuild=True``
    (default) clears the existing graph first. ``use_schema=True`` steers
    extraction with the KB's approved intent schema when one is set; free-form
    otherwise. Returns ``{findings_scanned, nodes_created, edges_created,
    node_count, edge_count}``."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await build_graph(ctx, max_findings=max_findings, rebuild=rebuild, use_schema=use_schema)


# --- Tenancy ---------------------------------------------------------------


def _projects_impl(store, include_archived: bool = False) -> dict:
    return {"projects": store.list_projects(include_archived=include_archived)}


@mcp.tool()
async def delapan_projects(include_archived: bool = False) -> dict:
    """List the caller's projects (by name) with their KBs — for client discovery.
    Each KB carries ``finding_count`` and ``last_finding_at`` (live findings only).
    Archived projects/KBs are omitted unless ``include_archived`` is true.
    Returns ``{"projects": [...]}``."""
    store = resolve_store()
    return _projects_impl(store, include_archived=include_archived)


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
    seed_demo_if_absent()
    mcp.run()


if __name__ == "__main__":
    main()
