"""Golden query sets — freeze query→verdict behavior with recorded vectors.

    fixture yaml + vectors.json ─► seed temp store ─► match_findings
                                                  └─► band → assess_coverage

Fully offline: query vectors are recorded, so no embedding client is involved.
A failure means retrieval behavior moved — inspect before re-recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from delapan.core.agent.preamble import assess_coverage, band_findings

GOLDEN = Path(__file__).parent / "golden"


def _sets():
    return sorted(GOLDEN.glob("*.yaml"))


@pytest.mark.parametrize("path", _sets(), ids=lambda p: p.stem)
@pytest.mark.asyncio
async def test_golden_set(path, store):
    spec = yaml.safe_load(path.read_text())
    vectors = json.loads(path.with_suffix(".vectors.json").read_text())

    org, pid = store.resolve_project(f"golden-{spec['name']}", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [
            {
                "id": f["id"], "org_id": org, "kb_id": kb, "title": f["title"],
                "content": f["content"], "category": f.get("category", "fact"),
                "confidence": 0.5, "tags": [], "provenance": [],
                "embedding": vectors[f["id"]],
            }
            for f in spec["findings"]
        ]
    )

    from delapan.core.config import get_config

    get_config.cache_clear()
    cfg = get_config().tiers
    for q in spec["queries"]:
        hits = await store.match_findings(
            kb, vectors[f"q:{q['query']}"], match_count=20, min_similarity=cfg.band3_min
        )
        bands = band_findings(hits, cfg)
        verdict = assess_coverage(bands, cfg)
        assert verdict == q["expect_verdict"], f"{path.stem}/{q['query']}: {verdict}"
        banded = [r["id"] for b in (1, 2, 3) for r in bands[b]]
        assert banded[: len(q["expect_top_ids"])] == q["expect_top_ids"], (
            f"{path.stem}/{q['query']}: {banded}"
        )
