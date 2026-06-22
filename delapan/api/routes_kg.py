"""Knowledge-graph routes — read AND mutate the per-KB graph over HTTP.

    GET    /api/projects/{p}/kbs/{k}/graph            ──► read_graph (focus/depth/caps)
    GET    /api/projects/{p}/kbs/{k}/graph/stats      ──► kg_stats
    GET    /api/projects/{p}/kbs/{k}/graph/schema     ──► kg_schema_view (intent+emergent)
    POST   /api/projects/{p}/kbs/{k}/graph/nodes      ──► upsert_kg_nodes
    PATCH  /api/projects/{p}/kbs/{k}/graph/nodes/{id} ──► update_kg_node
    DELETE /api/projects/{p}/kbs/{k}/graph/nodes/{id} ──► delete_kg_node (+incident edges)
    DELETE /api/projects/{p}/kbs/{k}/graph/edges/{id} ──► delete_kg_edge
    POST   /api/projects/{p}/kbs/{k}/graph/edges      ──► upsert_kg_edges

Wire format (the contract the control-panel frontend is built against):
node = ``{id, type, label, properties, grounded_in, created_at}``; edge =
``{id, source, target, relation, properties, grounded_in, created_at}`` —
``source``/``target`` map from the store's ``source_node_id``/``target_node_id``.
Node-create embeddings are best-effort: no OPENAI_API_KEY → insert unembedded.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from delapan.api.deps import resolve_kb_or_404
from delapan.core.agent.concept_doc import synthesize_concept_doc
from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import get_settings
from delapan.core.knowledge_graph.service import kg_schema_view, read_graph

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}/graph")


def _wire_node(n: dict) -> dict:
    return {
        "id": n.get("id"),
        "type": n.get("type"),
        "label": n.get("label"),
        "properties": n.get("properties") or {},
        "grounded_in": n.get("grounded_in") or [],
        "created_at": n.get("created_at"),
    }


def _wire_edge(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "source": e.get("source_node_id"),
        "target": e.get("target_node_id"),
        "relation": e.get("relation"),
        "properties": e.get("properties") or {},
        "grounded_in": e.get("grounded_in") or [],
        "created_at": e.get("created_at"),
    }


# --- reads -------------------------------------------------------------------


@router.get("")
def get_graph(
    project: str,
    kb: str,
    focus: str | None = None,
    depth: int = 2,
    node_cap: int = 500,
    edge_cap: int = 2000,
) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    graph = read_graph(
        store, ctx.kb_id, focus=focus, depth=depth, node_cap=node_cap, edge_cap=edge_cap
    )
    return {
        "nodes": [_wire_node(n) for n in graph.get("nodes") or []],
        "edges": [_wire_edge(e) for e in graph.get("edges") or []],
    }


@router.get("/stats")
def get_stats(project: str, kb: str) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    return store.kg_stats(ctx.kb_id)


@router.get("/schema")
def get_schema(project: str, kb: str) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    return kg_schema_view(store, ctx.kb_id)


# --- node mutations ----------------------------------------------------------


class NodeIn(BaseModel):
    type: str
    label: str
    properties: dict = Field(default_factory=dict)
    grounded_in: list[str] = Field(default_factory=list)


class NodesBody(BaseModel):
    nodes: list[NodeIn]


class NodePatch(BaseModel):
    label: str | None = None
    type: str | None = None
    properties: dict | None = None
    grounded_in: list[str] | None = None


@router.post("/nodes")
async def post_nodes(project: str, kb: str, body: NodesBody) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    embeddings: list[list[float]] | None = None
    if get_settings().openai_api_key:
        try:
            embeddings = await embed_batch([n.label for n in body.nodes])
        except Exception:  # noqa: BLE001 — embeddings are best-effort; never 500
            embeddings = None
    rows: list[dict] = []
    for i, n in enumerate(body.nodes):
        row: dict = {
            "org_id": ctx.org_id,
            "type": n.type,
            "label": n.label,
            "properties": n.properties,
            "grounded_in": n.grounded_in,
        }
        if embeddings is not None:
            row["embedding"] = embeddings[i]
        rows.append(row)
    ids = await store.upsert_kg_nodes(ctx.kb_id, rows)
    return {"ids": ids}


@router.patch("/nodes/{node_id}")
async def patch_node(project: str, kb: str, node_id: str, body: NodePatch) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    existing = store.get_kg_node(ctx.kb_id, node_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    await store.update_kg_node(
        ctx.kb_id,
        node_id,
        properties=body.properties if body.properties is not None else existing["properties"],
        grounded_in=body.grounded_in,
        label=body.label,
        type=body.type,
    )
    updated = store.get_kg_node(ctx.kb_id, node_id) or existing
    return {"node": _wire_node(updated)}


@router.delete("/nodes/{node_id}")
def delete_node(project: str, kb: str, node_id: str) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    result = store.delete_kg_node(ctx.kb_id, node_id)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    return result


@router.post("/nodes/{node_id}/concept-doc")
async def concept_doc(project: str, kb: str, node_id: str) -> JSONResponse:
    if not get_settings().ai_gateway_api_key:
        return JSONResponse(status_code=503, content={"error": "llm unavailable"})
    ctx, store = resolve_kb_or_404(project, kb)
    try:
        doc = await synthesize_concept_doc(store, ctx.kb_id, node_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
    except Exception:  # noqa: BLE001 — gateway/LLM failure (402/429/5xx, timeout)
        # Return a clean 503 the SPA already handles (toast + stay in deterministic
        # mode). An uncaught 500 would lack CORS headers, so the browser reads it
        # as a network error and flips the whole panel to offline mock data.
        return JSONResponse(status_code=503, content={"error": "llm unavailable"})
    return JSONResponse(doc)


# --- edge mutations ----------------------------------------------------------


class EdgeIn(BaseModel):
    source: str
    target: str
    relation: str
    properties: dict = Field(default_factory=dict)
    grounded_in: list[str] = Field(default_factory=list)


class EdgesBody(BaseModel):
    edges: list[EdgeIn]


@router.post("/edges")
async def post_edges(project: str, kb: str, body: EdgesBody) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    rows = [
        {
            "org_id": ctx.org_id,
            "source_node_id": e.source,
            "target_node_id": e.target,
            "relation": e.relation,
            "properties": e.properties,
            "grounded_in": e.grounded_in,
        }
        for e in body.edges
    ]
    inserted = await store.upsert_kg_edges(ctx.kb_id, rows)
    return {"inserted": inserted}


@router.delete("/edges/{edge_id}")
def delete_edge(project: str, kb: str, edge_id: str) -> dict:
    ctx, store = resolve_kb_or_404(project, kb)
    result = store.delete_kg_edge(ctx.kb_id, edge_id)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=f"edge not found: {edge_id}")
    return result
