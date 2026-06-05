"""Knowledge-graph build: findings → entities/relations → kg_nodes/kg_edges.

    findings ─► extractor (LLM) ─► KGExtraction ─► builder (dedupe + upsert) ─► KG

The write side of the already-shipped `/v1/graph` + `delapan_graph` reads.
Pure extraction/resolution logic lives here; Supabase persistence is in builder.
"""
