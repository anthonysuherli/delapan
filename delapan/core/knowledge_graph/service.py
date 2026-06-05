"""KG read helpers — the read side of the populated graph.

    kb_id ──► read_graph (full / focus-BFS)   kg_stats (counts)   kg_schema (ontology)

Reads through the Store seam, scoped to `kb_id` (the MCP layer resolves tenancy
first). `read_graph` mirrors the `/v1/graph` endpoint's shape so the MCP and
public surfaces return the same structure.

Open-core note: the local Store dedupes nodes by exact ``(type, label)`` and has
no ``aliases``/``merge_history`` columns, so the entity-resolution surface
fields collapse to ``aliases=[]`` / ``merge_count=0``. The shape is preserved so
the read contract is identical across tiers.
"""

from __future__ import annotations

from delapan.store import Store

_DIGEST_NODE_CAP = 5000
_DIGEST_EDGE_CAP = 5000


def _shape_nodes(rows: list) -> list[dict]:
    """Surface entity-resolution fields on read: ensure `aliases` is a list and
    replace the raw `merge_history` blob with a compact `merge_count`. The local
    Store omits both columns, so this degrades to empty/zero — same shape."""
    shaped: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        n = dict(r)
        n["aliases"] = list(n.get("aliases") or [])
        n["merge_count"] = len(n.pop("merge_history", None) or [])
        shaped.append(n)
    return shaped


def read_graph(
    store: Store,
    kb_id: str,
    *,
    focus: str | None = None,
    depth: int = 2,
    node_cap: int = 500,
    edge_cap: int = 2000,
) -> dict:
    """Full graph (capped) or a depth-bounded BFS subgraph around `focus`."""
    if not focus:
        graph = store.get_kg_subgraph(kb_id, node_cap=node_cap, edge_cap=edge_cap)
        return {"nodes": _shape_nodes(graph.get("nodes") or []), "edges": graph.get("edges") or []}

    needle = focus.strip().lower()
    seed_ids = [
        n["id"]
        for n in store.list_kg_nodes(kb_id, limit=node_cap)
        if isinstance(n, dict) and needle in str(n.get("label", "")).lower()
    ][:10]
    if not seed_ids:
        return {"nodes": [], "edges": []}

    graph = store.get_kg_subgraph(
        kb_id, seed_node_ids=seed_ids, node_cap=node_cap, edge_cap=edge_cap, depth=depth
    )
    return {"nodes": _shape_nodes(graph.get("nodes") or []), "edges": graph.get("edges") or []}


def kg_stats(store: Store, kb_id: str) -> dict:
    """Node/edge totals + counts by node type and by relation."""
    return store.kg_stats(kb_id)


def kg_schema(store: Store, kb_id: str) -> dict:
    """The ontology actually present in the KG: distinct node types + relations."""
    stats = store.kg_stats(kb_id)
    return {
        "node_types": sorted(stats["by_type"].keys()),
        "relations": sorted(stats["by_relation"].keys()),
        "node_count": stats["node_count"],
        "edge_count": stats["edge_count"],
    }


# --- KG intent schema (the user-approved target ontology) -------------------
# Stored versioned behind the Store seam: `set` inserts a new version, `get`
# reads the highest. The build reads `get` to steer extraction; `kg_schema_view`
# pairs the INTENT with the EMERGENT ontology so drift between what the user
# asked for and what the graph actually contains is visible.


def get_kg_intent(store: Store, kb_id: str) -> dict | None:
    """The KB's highest-version approved KG intent schema, or None if never set.

    Unwraps the Store's ``{version, schema}`` row into the flat schema dict (with
    ``version`` folded in) that the builder and the schema view expect."""
    row = store.get_kg_intent(kb_id)
    if not row:
        return None
    schema = row.get("schema") or {}
    if isinstance(schema, dict):
        schema = {**schema, "version": row.get("version", schema.get("version", 1))}
        return schema
    return None


def set_kg_intent(store: Store, kb_id: str, org_id: str, schema: dict) -> dict:
    """Persist an approved schema as the next version (never overwrites history).
    Returns the stored schema with its assigned `version`."""
    row = store.set_kg_intent(org_id, kb_id, schema)
    return {**schema, "version": row.get("version", 1)}


def kg_schema_view(store: Store, kb_id: str) -> dict:
    """Both ontologies for the KB: the approved `intent` (or None) and the
    `emergent` ontology actually present in the built graph — so drift is visible."""
    return {"intent": get_kg_intent(store, kb_id), "emergent": kg_schema(store, kb_id)}


# --- Activity digest: the "most active" entities/relations ------------------
# There is no stored activity counter, so "most active" is read structurally: a
# node's *degree* (count of incident edges) and a relation's *frequency*.
# `kg_digest` aggregates across one or more KBs.


def kg_digest(store: Store, kb_ids: list[str], *, top_n: int = 8) -> dict:
    """Top entities (by edge degree) + top relations (by frequency) across KBs.

    Returns ``{node_count, edge_count, top_entities, top_relations}`` where
    ``top_entities`` is ``[{label, type, degree}]`` and ``top_relations`` is
    ``[{relation, count}]`` — both already truncated to ``top_n`` and sorted
    desc. Empty-but-well-formed when ``kb_ids`` is empty or the KG is unbuilt."""
    if not kb_ids:
        return {"node_count": 0, "edge_count": 0, "top_entities": [], "top_relations": []}

    nodes: list[dict] = []
    edges: list[dict] = []
    for kb_id in kb_ids:
        graph = store.get_kg_subgraph(kb_id, node_cap=_DIGEST_NODE_CAP, edge_cap=_DIGEST_EDGE_CAP)
        nodes.extend(graph.get("nodes") or [])
        edges.extend(graph.get("edges") or [])

    by_id: dict[str, dict] = {str(r["id"]): r for r in nodes if isinstance(r, dict) and r.get("id")}

    degree: dict[str, int] = {}
    rel_count: dict[str, int] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        for endpoint in (e.get("source_node_id"), e.get("target_node_id")):
            if endpoint:
                key = str(endpoint)
                degree[key] = degree.get(key, 0) + 1
        rel = str(e.get("relation") or "unknown")
        rel_count[rel] = rel_count.get(rel, 0) + 1

    # Rank entities by degree; isolated nodes (degree 0) still rank below any
    # connected node but stay eligible so a freshly built, edge-light graph
    # still yields a digest. Tie-break on label for stable output.
    ranked = sorted(
        by_id.items(),
        key=lambda kv: (degree.get(kv[0], 0), str(kv[1].get("label", ""))),
        reverse=True,
    )
    top_entities = [
        {
            "label": str(n.get("label", "")),
            "type": str(n.get("type", "")),
            "degree": degree.get(node_id, 0),
        }
        for node_id, n in ranked[:top_n]
    ]
    top_relations = [
        {"relation": rel, "count": cnt}
        for rel, cnt in sorted(rel_count.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[
            :top_n
        ]
    ]
    return {
        "node_count": len(by_id),
        "edge_count": len(edges),
        "top_entities": top_entities,
        "top_relations": top_relations,
    }


def focus_subgraph(nodes: list[dict], edges: list[dict], *, max_nodes: int = 20) -> dict:
    """Top-degree induced subgraph — pure (no Store).

    Keep the ``max_nodes`` highest-degree nodes (degree = incident-edge count,
    same structural notion as ``kg_digest``) and only the edges whose BOTH
    endpoints survive. Keeps the report's kg_diagram legible (the renderer caps
    at 40 nodes and only labels when ≤18) instead of drawing the whole hairball.
    Tie-break on label/id for deterministic output."""
    if not nodes:
        return {"nodes": [], "edges": []}

    degree: dict[str, int] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        for endpoint in (e.get("source_node_id"), e.get("target_node_id")):
            if endpoint:
                key = str(endpoint)
                degree[key] = degree.get(key, 0) + 1

    ranked = sorted(
        (n for n in nodes if isinstance(n, dict) and n.get("id")),
        key=lambda n: (degree.get(str(n["id"]), 0), str(n.get("label", "")) or str(n["id"])),
        reverse=True,
    )
    kept = ranked[:max_nodes]
    kept_ids = {str(n["id"]) for n in kept}
    induced = [
        e
        for e in edges
        if isinstance(e, dict)
        and str(e.get("source_node_id")) in kept_ids
        and str(e.get("target_node_id")) in kept_ids
    ]
    return {"nodes": kept, "edges": induced}


def kg_focus_subgraph(store: Store, kb_id: str, *, max_nodes: int = 20) -> dict:
    """Fetch the KB's graph and reduce it to a legible top-degree subgraph.

    Thin Store wrapper around the pure ``focus_subgraph``; returns empty when the
    KB has no graph (the report then simply omits the KG exhibits)."""
    graph = read_graph(store, kb_id)
    return focus_subgraph(graph.get("nodes") or [], graph.get("edges") or [], max_nodes=max_nodes)


def render_kg_context(digest: dict) -> str:
    """Render a `kg_digest` as a tight written block for the conversation.

    Mirrors the auditable, titles-not-bodies style of the preamble digest: a
    one-line header (counts) then the most-active entities and relations. Returns
    a single "not built yet" line when the KG is empty."""
    n, m = digest.get("node_count", 0), digest.get("edge_count", 0)
    if not n:
        return "Knowledge graph: not built yet — run `delapan_build_graph` to populate it."

    lines = [f"Knowledge graph: {n} entities, {m} relations."]
    ents = digest.get("top_entities") or []
    if ents:
        named = ", ".join(
            f"{e['label']} ({e['degree']})" if e.get("degree") else str(e["label"]) for e in ents
        )
        lines.append(f"Most connected: {named}.")
    rels = digest.get("top_relations") or []
    if rels:
        named = ", ".join(f"{r['relation']} ×{r['count']}" for r in rels)
        lines.append(f"Key relations: {named}.")
    return "\n".join(lines)
