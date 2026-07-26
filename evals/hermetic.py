"""Offline retrieval metrics over golden sets — the pytest eval tier.

    golden yaml + vectors ──► seed store ─► match ─► band ─► IR metrics + calibration
"""

from __future__ import annotations

from delapan.core.agent.preamble import assess_coverage, band_findings
from delapan.core.config import get_config
from evals.scoring.retrieval import mrr, ndcg_at_k, precision_at_k, verdict_calibration


async def golden_retrieval_metrics(spec: dict, vectors: dict, store) -> dict:
    org, pid = store.resolve_project(f"evals-golden-{spec['name']}", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [
            {"id": f["id"], "org_id": org, "kb_id": kb, "title": f["title"],
             "content": f["content"], "category": f.get("category", "fact"),
             "confidence": 0.5, "tags": [], "provenance": [],
             "embedding": vectors[f["id"]]}
            for f in spec["findings"]
        ]
    )
    get_config.cache_clear()
    cfg = get_config().tiers

    per_query: list[dict] = []
    cal_records: list[dict] = []
    for q in spec["queries"]:
        hits = await store.match_findings(
            kb, vectors[f"q:{q['query']}"], match_count=20, min_similarity=cfg.band3_min
        )
        bands = band_findings(hits, cfg)
        verdict = assess_coverage(bands, cfg)
        banded = [r["id"] for b in (1, 2, 3) for r in bands[b]]
        gold = set(q["expect_top_ids"])
        per_query.append(
            {"query": q["query"], "verdict": verdict, "expected_verdict": q["expect_verdict"],
             "p_at_3": precision_at_k(banded, gold, 3), "mrr": mrr(banded, gold),
             "ndcg_at_5": ndcg_at_k(banded, gold, 5)}
        )
        cal_records.append(
            {"verdict": verdict, "gold_ids": list(gold), "injected_ids": banded}
        )
    return {"per_query": per_query, "calibration": verdict_calibration(cal_records)}
