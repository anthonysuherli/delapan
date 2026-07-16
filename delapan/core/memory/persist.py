"""resolve_and_persist — the single finding-persist path.

    candidates ─► render+embed ─► resolve ─► apply (insert/update/delete) ─► log
                                                         └─► ResolutionOutcome

Replaces the duplicated persist block in the explore call sites. With
``memory.enabled is False`` (or no candidates) it is pure ADD — byte-for-byte
today's append behavior.
"""

from __future__ import annotations

import logging

from delapan.core.agent.state import TenantContext
from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import AppConfig
from delapan.core.exploration.merger import confidence_from_sources
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import normalize_provenance, render_content
from delapan.core.memory.models import ResolutionEvent, ResolutionOp, ResolutionOutcome
from delapan.core.memory.resolver import resolve
from delapan.store import Store

logger = logging.getLogger(__name__)


def _row_from_candidate(ctx: TenantContext, f: Finding, embedding: list[float]) -> dict:
    """Build an insert_findings row from a candidate Finding (parity with the old
    explore persist block: content rendered to a markdown body)."""
    return {
        "org_id": ctx.org_id,
        "kb_id": ctx.kb_id,
        "title": f.title,
        "content": render_content(f.content),
        "category": f.category,
        "confidence": float(f.confidence) if f.confidence is not None else None,
        "tags": list(f.tags or []),
        "provenance": normalize_provenance(f.provenance),
        "embedding": embedding,
    }


def _merge_provenance(existing: list[dict], new: list[dict]) -> list[dict]:
    """Union by url (existing first), preserving non-url entries."""
    seen: set[str] = set()
    out: list[dict] = []
    for p in [*(existing or []), *(new or [])]:
        url = p.get("url", "") if isinstance(p, dict) else ""
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        out.append(p)
    return out


async def resolve_and_persist(
    ctx: TenantContext, store: Store, candidates: list[Finding], cfg: AppConfig
) -> ResolutionOutcome:
    """Resolve each candidate against existing findings, then apply + log.

    Returns the affected finding ids (added ∪ updated) for the synopsis/KG
    schedulers. NOOP/DELETE contribute no affected ids."""
    if not candidates:
        return ResolutionOutcome()

    contents = [render_content(f.content) for f in candidates]
    embeddings = await embed_batch(contents)

    # Kill-switch / fast path: pure ADD, no resolution, no events.
    if not cfg.memory.enabled:
        rows = [_row_from_candidate(ctx, f, emb) for f, emb in zip(candidates, embeddings)]
        ids = await store.insert_findings(rows)
        return ResolutionOutcome(affected_finding_ids=ids)

    decisions = await resolve(store, ctx.kb_id, candidates, embeddings, cfg.memory)

    affected: list[str] = []
    events: list[ResolutionEvent] = []
    add_rows: list[dict] = []
    for f, emb, d in zip(candidates, embeddings, decisions):
        if d.op == ResolutionOp.ADD:
            add_rows.append(_row_from_candidate(ctx, f, emb))
            events.append(ResolutionEvent(op="ADD", candidate_title=f.title, reason=d.reason))
        elif d.op == ResolutionOp.NOOP:
            events.append(
                ResolutionEvent(
                    op="NOOP",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    reason=d.reason,
                )
            )
        elif d.op == ResolutionOp.DELETE:
            try:
                store.delete_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — a stale target is not fatal
                logger.debug("resolution DELETE: target %s absent", d.target_finding_id)
            events.append(
                ResolutionEvent(
                    op="DELETE",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    reason=d.reason,
                )
            )
        elif d.op == ResolutionOp.UPDATE:
            try:
                target = store.get_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — target vanished → ADD instead
                add_rows.append(_row_from_candidate(ctx, f, emb))
                events.append(
                    ResolutionEvent(
                        op="ADD", candidate_title=f.title, reason="update target missing; added"
                    )
                )
                continue
            merged_prov = _merge_provenance(
                target.get("provenance") or [], normalize_provenance(f.provenance)
            )
            source_count = len({p.get("url") for p in merged_prov if p.get("url")}) or 1
            await store.update_finding(
                ctx.kb_id,
                d.target_finding_id,
                content=render_content(f.content),
                confidence=confidence_from_sources(source_count),
                provenance=merged_prov,
                embedding=emb,
                title=f.title,
            )
            affected.append(d.target_finding_id)
            events.append(
                ResolutionEvent(
                    op="UPDATE",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    reason=d.reason,
                )
            )

    if add_rows:
        affected.extend(await store.insert_findings(add_rows))
    if events:
        await store.insert_resolution_events(ctx.kb_id, [e.model_dump() for e in events])
    return ResolutionOutcome(affected_finding_ids=affected, events=events)
