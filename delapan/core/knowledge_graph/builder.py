"""KG builder — gather findings, extract a graph, dedupe + persist.

    findings ─► extract_graph ─► collapse nodes ─► upsert_kg_nodes ─► kg_nodes
                                              └─► resolve edge labels → ids ─► kg_edges

Writes through the Store seam: the `/agent` + MCP entry paths already verified KB
ownership before calling. The Store's ``upsert_kg_nodes`` is itself insert-or-merge
by exact ``(type, label)`` and returns ids in input order, so the in-pass
``label → id`` map falls out of one call. Edges are resolved against that map and
the Store de-duplicates them on ``(source, target, relation)``.

Open-core note: the local Store dedupes nodes by exact ``(type, label)`` (not the
pgvector ``match_kg_nodes`` near-match the cloud tier uses) and carries no
``aliases``/``merge_history`` columns — so the in-memory alias/merge-history
accumulation survives the collapse but is dropped at persist. The graph shape and
grounding are identical across tiers.
"""

from __future__ import annotations

import asyncio
import logging

from delapan.core.agent.state import TenantContext
from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import get_config
from delapan.core.knowledge_graph import service as kg_service
from delapan.core.knowledge_graph.extractor import extract_graph
from delapan.core.knowledge_graph.models import KGEdgeExtract, KGNodeExtract
from delapan.core.knowledge_graph.schema import KGSchema
from delapan.store import Store, get_store

logger = logging.getLogger(__name__)


def _norm(label: str) -> str:
    """Normalize a label for exact in-batch matching: trim, lower, collapse ws."""
    return " ".join(label.strip().lower().split())


def _collapse_nodes(nodes: list[KGNodeExtract]) -> list[KGNodeExtract]:
    """Merge extracted nodes sharing a normalized label (first-seen order). The
    first node's `type` wins; properties are shallow-merged with the first
    winning on key conflicts. The dropped node's surface label (plus any aliases
    it already carried) is accumulated onto the survivor's `aliases`, ordered and
    de-duped (the survivor's own canonical label is never added as its own alias)."""
    merged: dict[str, KGNodeExtract] = {}
    for n in nodes:
        key = _norm(n.label)
        if not key:
            continue
        if key in merged:
            existing = merged[key]
            props = {**n.properties, **existing.properties}
            grounded = list(
                dict.fromkeys([*existing.grounded_in, *n.grounded_in])
            )  # union, ordered
            aliases = list(
                dict.fromkeys(
                    a
                    for a in [*existing.aliases, n.label, *n.aliases]
                    if a and a != existing.label  # keep variant surface forms, not the exact label
                )
            )
            merged[key] = existing.model_copy(
                update={"properties": props, "grounded_in": grounded, "aliases": aliases}
            )
        else:
            merged[key] = n
    return list(merged.values())


def _resolve_edges(edges: list[KGEdgeExtract], label_to_id: dict[str, str]) -> list[dict]:
    """Map each edge's source/target labels to node ids via `label_to_id`.
    Drops dangling endpoints + self-loops; de-duplicates on
    (source_id, target_id, relation)."""
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for e in edges:
        sid = label_to_id.get(_norm(e.source))
        tid = label_to_id.get(_norm(e.target))
        if not sid or not tid or sid == tid:
            continue
        key = (sid, tid, e.relation)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "source_node_id": sid,
                "target_node_id": tid,
                "relation": e.relation,
                "properties": e.properties,
                "grounded_in": e.grounded_in,
            }
        )
    return out


def _load_intent(store: Store, kb_id: str) -> KGSchema | None:
    """Read the KB's highest-version intent schema, if any. Defensive — a malformed
    stored schema logs and yields None (free-form fallback), never raises."""
    raw = kg_service.get_kg_intent(store, kb_id)
    if not raw:
        return None
    try:
        return KGSchema.model_validate(raw)
    except Exception:  # noqa: BLE001 — a bad stored schema must not sink the build
        logger.warning("KB %s has a malformed KG intent schema; building free-form", kb_id)
        return None


def _gather_findings(
    store: Store, kb_id: str, *, finding_ids: list[str] | None, max_findings: int
) -> list[dict]:
    """Load findings (with content) for extraction.

    The Store's list view omits ``content``, so the full-rebuild path lists ids
    then hydrates each via ``get_finding``; the incremental path hydrates the
    given ids directly. Missing ids (stale deltas) are skipped, not fatal."""
    if finding_ids:
        ids = finding_ids
    else:
        listing = store.list_findings(kb_id, limit=max_findings)
        rows = listing.get("findings", []) if isinstance(listing, dict) else []
        ids = [r["id"] for r in rows if isinstance(r, dict) and r.get("id")]
    findings: list[dict] = []
    for fid in ids:
        try:
            findings.append(store.get_finding(kb_id, fid))
        except Exception:  # noqa: BLE001 — a stale/absent id just drops from the delta
            logger.debug("KG build: finding %s not found in kb=%s", fid, kb_id)
    return findings


async def build_graph(
    ctx: TenantContext,
    *,
    max_findings: int | None = None,
    rebuild: bool = True,
    use_schema: bool = True,
    finding_ids: list[str] | None = None,
    store: Store | None = None,
) -> dict:
    """Extract entities/relations from the KB's findings and persist them.

    `rebuild=True` (default) clears the KB's existing nodes/edges first — a clean,
    predictable manual rebuild. The Store's node upsert still collapses
    near-duplicate entities (by exact ``(type, label)``) within the pass.
    `use_schema=True` (default) loads the KB's approved intent schema (if one was
    set via `set_kg_intent`) and steers extraction with it as SOFT guidance; with
    no schema set, or `use_schema=False`, extraction stays free-form.

    **Incremental:** pass `finding_ids` to extract over *only* those findings and
    APPEND them to the existing graph (forces `rebuild=False`) — the Store's node
    upsert folds new entities into existing nodes. This is what the auto-trigger
    after `explore` uses, so the graph grows with the KB instead of needing a full
    manual rebuild every time.
    Returns created counts + the resulting totals.
    """
    cfg = get_config().knowledge_graph
    store = store or get_store(
        getattr(ctx, "access_token", None), org_id=getattr(ctx, "org_id", None)
    )

    if finding_ids:
        rebuild = False  # incremental: never clear when appending a delta
    findings = _gather_findings(
        store,
        ctx.kb_id,
        finding_ids=finding_ids,
        max_findings=max_findings or cfg.max_findings,
    )
    if not findings:
        return {
            "findings_scanned": 0,
            "nodes_created": 0,
            "edges_created": 0,
            "node_count": 0,
            "edge_count": 0,
        }

    schema = _load_intent(store, ctx.kb_id) if use_schema else None
    extraction = await extract_graph(findings, cfg, schema)
    nodes = _collapse_nodes(extraction.nodes)[: cfg.max_nodes]

    if rebuild:
        store.clear_kg(ctx.kb_id)

    stats_before = store.kg_stats(ctx.kb_id)
    label_to_id: dict[str, str] = {}
    if nodes:
        embeddings = await embed_batch([nd.label for nd in nodes])
        node_rows = [
            {
                "type": nd.type,
                "label": nd.label,
                "properties": nd.properties,
                "grounded_in": nd.grounded_in,
                "embedding": emb,
            }
            for nd, emb in zip(nodes, embeddings)
        ]
        ids = await store.upsert_kg_nodes(ctx.kb_id, node_rows)
        for nd, nid in zip(nodes, ids):
            label_to_id[_norm(nd.label)] = nid

    edges = _resolve_edges(extraction.edges, label_to_id)[: cfg.max_edges]
    edges_created = 0
    if edges:
        edges_created = await store.upsert_kg_edges(ctx.kb_id, edges)

    stats_after = store.kg_stats(ctx.kb_id)
    nodes_created = max(stats_after["node_count"] - stats_before["node_count"], 0)

    return {
        "findings_scanned": len(findings),
        "nodes_created": nodes_created,
        "edges_created": edges_created,
        "node_count": stats_after["node_count"],
        "edge_count": stats_after["edge_count"],
    }


# -----------------------------------------------------------------------------
# Auto-trigger: grow the graph incrementally after each exploration
# -----------------------------------------------------------------------------

_KG_BG_TASKS: set[asyncio.Task] = set()


async def _auto_kg_update(
    ctx: TenantContext, finding_ids: list[str], *, store: Store | None = None
) -> None:
    """Append freshly-explored findings to the KG, if the KB has a co-designed
    intent schema. Gated on an intent so we only auto-grow graphs the user has
    actually scoped — a free-form auto-build would drift unpredictably. Best-
    effort: never raises (mirrors the synopsis rebuild)."""
    try:
        store = store or get_store(
            getattr(ctx, "access_token", None), org_id=getattr(ctx, "org_id", None)
        )
        if not kg_service.get_kg_intent(store, ctx.kb_id):
            return  # no approved ontology → leave the (manual) build to the user
        result = await build_graph(ctx, finding_ids=finding_ids, use_schema=True, store=store)
        logger.info(
            "auto KG update kb=%s: +%s nodes, +%s edges (%s findings)",
            ctx.kb_id,
            result.get("nodes_created"),
            result.get("edges_created"),
            len(finding_ids),
        )
    except Exception:  # noqa: BLE001 — auto-grow is best-effort, never breaks a turn
        logger.exception("auto KG update failed for kb=%s", ctx.kb_id)


def schedule_kg_update(
    ctx: TenantContext, finding_ids: list[str], *, store: Store | None = None
) -> None:
    """Fire-and-forget incremental KG update that won't be GC'd mid-flight (holds
    a strong ref until done, like `synopsis.schedule_rebuild`). No-op without new
    findings."""
    if not finding_ids:
        return
    task = asyncio.create_task(_auto_kg_update(ctx, finding_ids, store=store))
    _KG_BG_TASKS.add(task)
    task.add_done_callback(_KG_BG_TASKS.discard)
