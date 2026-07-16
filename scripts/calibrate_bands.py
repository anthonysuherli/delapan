"""Propose coverage-band thresholds for the live embedding model.

    on-topic queries ──► same-KB similarities  ─┐
                                                ├─► distributions ─► proposed bands
    same queries ─────► other-KB similarities ─┘

Bands gate QUERY→finding similarity, so that is what we sample: a KB's own
queries as the positive class, the same queries against a DIFFERENT KB as the
off-topic negative class. Output is advisory — a human picks the thresholds and
the golden sets prove them.

Cloud-tier note: a bare `get_store()` needs a bearer token/org on the cloud
backend, so tenancy is resolved via `resolve_tenant` (same helper the in-process
MCP entry path uses) rather than calling `get_store()` unauthenticated.

    uv run python scripts/calibrate_bands.py delapan master --against actuary/ifrs17-hk
"""

from __future__ import annotations

import argparse
import asyncio
import statistics

from delapan.core.clients.embeddings import embed_batch
from delapan.mcp.tenancy import resolve_tenant
from delapan.store import Store, get_store

DEFAULT_QUERIES = [
    "delapan engine architecture: findings pipeline and preamble assembly",
    "how does coverage banding decide rich vs sparse vs gap",
    "what orchestration patterns did production systems converge on",
]


async def _sims(store: Store, kb_id: str, embs: list[list[float]]) -> list[float]:
    out: list[float] = []
    for e in embs:
        hits = await store.match_findings(kb_id, e, match_count=20, min_similarity=0.0)
        out.extend(h["similarity"] for h in hits)
    return out


def _describe(label: str, sims: list[float]) -> None:
    if not sims:
        print(f"{label}: (no hits)")
        return
    qs = statistics.quantiles(sims, n=100)
    print(
        f"{label}: n={len(sims)} min={min(sims):.3f} p25={qs[24]:.3f} "
        f"median={statistics.median(sims):.3f} p75={qs[74]:.3f} p95={qs[94]:.3f} max={max(sims):.3f}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("kb")
    ap.add_argument("--against", required=True, help="off-topic KB as project/kb")
    ap.add_argument("--query", action="append", default=None)
    args = ap.parse_args()

    queries = args.query or DEFAULT_QUERIES
    embs = await embed_batch(queries)

    ctx = resolve_tenant(args.project, args.kb, create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id) if ctx.access_token else get_store()
    kb_id = ctx.kb_id

    off_project, off_kb = args.against.split("/", 1)
    off_ctx = resolve_tenant(off_project, off_kb, create=False)
    off_kb_id = off_ctx.kb_id

    on = await _sims(store, kb_id, embs)
    off = await _sims(store, off_kb_id, embs)
    _describe("on-topic (positive)", on)
    _describe("off-topic (negative)", off)

    if on and off:
        off_p95 = statistics.quantiles(off, n=100)[94]
        on_median = statistics.median(on)
        print("\nproposed (advisory):")
        print(f"  band1_min: {max(off_p95, on_median):.2f}   # above off-topic noise")
        print(f"  band2_min: {off_p95:.2f}   # off-topic p95 — the separation point")
        print(f"  band3_min: {(off_p95 * 0.8):.2f}   # weak-but-plausible floor")
        print("\nValidate with: uv run pytest tests/test_golden_sets.py -v")


if __name__ == "__main__":
    asyncio.run(main())
