from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_kg_nodes_edges_and_intent(store):
    org_id, project_id = store.resolve_project("kg", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    ids = await store.upsert_kg_nodes(
        kb_id,
        [
            {
                "type": "Concept",
                "label": "Alpha",
                "properties": {},
                "grounded_in": [],
                "embedding": [0.0] * 1536,
            },
            {
                "type": "Concept",
                "label": "Beta",
                "properties": {},
                "grounded_in": [],
                "embedding": [0.1] * 1536,
            },
        ],
    )
    assert len(ids) == 2
    n = await store.upsert_kg_edges(
        kb_id,
        [
            {
                "source_node_id": ids[0],
                "target_node_id": ids[1],
                "relation": "rel",
                "properties": {},
                "grounded_in": [],
            },
        ],
    )
    assert n == 1
    stats = store.kg_stats(kb_id)
    assert stats["node_count"] == 2 and stats["edge_count"] == 1
    store.set_kg_intent(org_id, kb_id, {"node_types": ["Concept"], "relation_types": ["rel"]})
    assert store.get_kg_intent(kb_id)["version"] == 1
