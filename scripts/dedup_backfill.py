"""Retire the duplicates a KB already accumulated — same decisions, different mechanics.

    live findings (oldest→newest) ─► resolve vs EARLIER rows ─► [BackfillOp] ─► apply
                                                                    │
                        dry-run by default: print ops + cost, touch nothing

A backfill candidate is already a live row, so the forward applier does not fit:
ADD keeps the row (no insert), NOOP/UPDATE/SUPERSEDE retire the loser in place via
invalidate_finding. Neighbors are restricted to rows EARLIER in replay order and
still live — otherwise every candidate matches itself at similarity ~1.0 — and
handed to resolve() via its neighbor_sets seam.

Cloud-tier note: a bare `get_store()` needs a bearer token/org on the cloud
backend, so tenancy is resolved via `resolve_tenant` (same helper the in-process
MCP entry path and scripts/calibrate_bands.py use) rather than calling
`get_store()` unauthenticated. `resolve()`'s cloud-vs-local behavior is
otherwise identical — the pure-ADD cloud guard lives in `resolve_and_persist`,
which this script does not call.

    uv run python scripts/dedup_backfill.py delapan master           # dry run
    uv run python scripts/dedup_backfill.py delapan master --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import AppConfig, get_config
from delapan.core.exploration.merger import confidence_from_sources
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import render_content
from delapan.core.memory.models import ResolutionEvent, ResolutionOp
from delapan.core.memory.persist import _distinct_urls, _merge_provenance
from delapan.core.memory.resolver import resolve
from delapan.mcp.tenancy import resolve_tenant
from delapan.store import Store, get_store

logger = logging.getLogger(__name__)


@dataclass
class BackfillOp:
    op: str                       # ADD | UPDATE | NOOP | SUPERSEDE
    candidate_id: str
    target_id: str | None = None
    reason: str = ""


def _restrict(hits: list[dict], candidate_id: str, allowed: set[str]) -> list[dict]:
    """Keep only earlier-in-replay, still-live neighbors — never the candidate itself."""
    return [h for h in hits if h["id"] != candidate_id and h["id"] in allowed]


def _as_finding(row: dict) -> Finding:
    """Build the resolver's candidate `Finding` from a live-row dict.

    ``Finding.content`` is typed as ``dict`` but some findings (e.g. plain
    ingested markdown, or the test fixtures) store a bare string — wrap it
    single-key so ``render_content`` still renders it byte-identical."""
    content = row["content"]
    if not isinstance(content, dict):
        content = {"text": content}
    return Finding(
        exploration_id="backfill", project_id="backfill",
        category=row.get("category") or "fact", title=row["title"],
        content=content, provenance=row.get("provenance") or [],
    )


async def plan_ops(store: Store, kb_id: str, cfg: AppConfig) -> list[BackfillOp]:
    """Decide one op per live finding, oldest first. Reads only — never writes.

    ``list_findings`` drops ``content``/``provenance`` in its list view (both
    tiers), so each row is re-fetched via ``get_finding`` before use. Every
    row — including the oldest, which has no earlier neighbor — is still run
    through ``resolve()``: an empty ``neighbor_sets`` entry short-circuits to
    ADD there without an LLM call, so nothing is special-cased here.
    """
    listed = store.list_findings(kb_id, limit=10_000)["findings"]
    rows = [store.get_finding(kb_id, r["id"]) for r in listed]
    rows.sort(key=lambda r: (r.get("created_at") or "", r["id"]))

    seen: set[str] = set()
    ops: list[BackfillOp] = []

    for row in rows:
        text = f"{row['title']}\n\n{render_content(row['content'])}"
        emb = (await embed_batch([text]))[0]
        hits = await store.match_findings(
            kb_id, emb, match_count=cfg.memory.neighbor_top_k + 5,
            min_similarity=cfg.memory.neighbor_min_similarity,
        )
        neighbors = _restrict(hits, row["id"], seen)[: cfg.memory.neighbor_top_k]

        candidate = _as_finding(row)
        decisions = await resolve(
            store, kb_id, [candidate], [emb], cfg.memory, neighbor_sets=[neighbors]
        )
        d = decisions[0]
        ops.append(
            BackfillOp(
                op=d.op.value, candidate_id=row["id"],
                target_id=d.target_finding_id, reason=d.reason,
            )
        )
        # Only a surviving row can be a neighbor for later candidates.
        if d.op == ResolutionOp.ADD:
            seen.add(row["id"])
        elif d.op in (ResolutionOp.UPDATE, ResolutionOp.SUPERSEDE):
            seen.discard(d.target_finding_id or "")
            seen.add(row["id"])
        # NOOP: the candidate retires; the target stays in `seen`.
    return ops


async def apply_ops(store: Store, kb_id: str, ops: list[BackfillOp]) -> dict:
    """Apply planned ops. ADD keeps the row; every other op retires one row."""
    counts: dict[str, int] = {}
    events: list[ResolutionEvent] = []
    for op in ops:
        counts[op.op] = counts.get(op.op, 0) + 1
        if op.op == "ADD" or not op.target_id:
            continue
        candidate = store.get_finding(kb_id, op.candidate_id)
        try:
            target = store.get_finding(kb_id, op.target_id)
        except Exception:  # noqa: BLE001 — target already retired by an earlier op
            logger.debug("backfill: target %s gone; skipping", op.target_id)
            continue

        if op.op == "NOOP":
            # The target survives and absorbs the duplicate's sources.
            merged = _merge_provenance(
                target.get("provenance") or [], candidate.get("provenance") or []
            )
            await store.update_finding(
                kb_id, op.target_id,
                confidence=max(
                    target.get("confidence") or 0.0,
                    confidence_from_sources(_distinct_urls(merged) or 1),
                ),
                provenance=merged,
            )
            await store.invalidate_finding(kb_id, op.candidate_id, superseded_by=op.target_id)
        elif op.op == "UPDATE":
            # The candidate (newer) survives and inherits the target's sources.
            merged = _merge_provenance(
                target.get("provenance") or [], candidate.get("provenance") or []
            )
            await store.update_finding(
                kb_id, op.candidate_id,
                confidence=confidence_from_sources(_distinct_urls(merged) or 1),
                provenance=merged,
            )
            await store.invalidate_finding(kb_id, op.target_id, superseded_by=op.candidate_id)
        elif op.op == "SUPERSEDE":
            # Contradiction: the target retires, its sources do NOT merge in.
            await store.invalidate_finding(kb_id, op.target_id, superseded_by=op.candidate_id)

        events.append(
            ResolutionEvent(
                op=op.op, candidate_title=candidate.get("title") or "",
                target_finding_id=op.target_id, new_finding_id=op.candidate_id,
                reason=f"backfill: {op.reason}",
            )
        )
    if events:
        await store.insert_resolution_events(kb_id, [e.model_dump() for e in events])
    return counts


def _estimate(n_rows: int, cfg: AppConfig) -> str:
    passes = max(1, n_rows)
    return (
        f"~{n_rows} embedding calls (batched) + up to ~{passes} resolution LLM calls "
        f"({cfg.memory.resolution_model})"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="Retire duplicate findings in an existing KB.")
    ap.add_argument("project")
    ap.add_argument("kb")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    get_config.cache_clear()
    cfg = get_config()

    ctx = resolve_tenant(args.project, args.kb, create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id) if ctx.access_token else get_store()
    kb_id = ctx.kb_id

    before = store.count_findings(kb_id)
    print(f"{args.project}/{args.kb}: {before} live findings")
    print(f"estimated cost: {_estimate(before, cfg)}\n")

    ops = await plan_ops(store, kb_id, cfg)
    counts: dict[str, int] = {}
    for op in ops:
        counts[op.op] = counts.get(op.op, 0) + 1
    print("planned ops:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for op in ops:
        if op.op != "ADD":
            print(f"  {op.op:<9} {op.candidate_id[:8]} → {(op.target_id or '')[:8]}  {op.reason}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply to execute.")
        return

    applied = await apply_ops(store, kb_id, ops)
    after = store.count_findings(kb_id)
    print(f"\napplied: {applied}")
    print(f"live findings: {before} → {after}")


if __name__ == "__main__":
    asyncio.run(main())
