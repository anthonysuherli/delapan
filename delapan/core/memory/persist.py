"""resolve_and_persist — the single finding-persist path.

    candidates ─► render+embed ─► resolve ─► apply (ADD/UPDATE/NOOP/SUPERSEDE) ─► log
                                                         └─► ResolutionOutcome

Replaces the duplicated persist block in the explore call sites. With
``memory.enabled is False`` (or no candidates) it is pure ADD — byte-for-byte
today's append behavior. The cloud tier reached write-primitive parity in plan
Task 10 (2026-07-16 migration applied to the live project) — resolution now
runs identically on both tiers, gated only by ``memory.enabled``.
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


def _distinct_urls(provenance: list[dict]) -> int:
    """Distinct source urls — url-less entries are kept but don't count as sources."""
    return len({p.get("url") for p in provenance if isinstance(p, dict) and p.get("url")})


async def resolve_and_persist(
    ctx: TenantContext, store: Store, candidates: list[Finding], cfg: AppConfig
) -> ResolutionOutcome:
    """Resolve each candidate against existing findings, then apply + log.

    Returns the affected finding ids (added ∪ new UPDATE/SUPERSEDE rows) for the
    synopsis/KG schedulers. NOOP contributes no affected id (the target is
    unretired and, absent a new url, untouched)."""
    if not candidates:
        return ResolutionOutcome()

    contents = [render_content(f.content) for f in candidates]
    embeddings = await embed_batch(contents)

    # Kill-switch: pure ADD, no resolution, no events.
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
            # Corroborate: the candidate's sources reinforce the existing finding.
            # No new url → nothing to corroborate → a true no-write (idempotent
            # re-runs leave the row byte-identical); the event is still logged.
            try:
                target = store.get_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — target vanished → ADD instead
                add_rows.append(_row_from_candidate(ctx, f, emb))
                events.append(
                    ResolutionEvent(
                        op="ADD", candidate_title=f.title,
                        reason="noop target vanished before write; added",
                    )
                )
                continue
            existing_prov = target.get("provenance") or []
            merged_prov = _merge_provenance(existing_prov, normalize_provenance(f.provenance))
            before_n, after_n = _distinct_urls(existing_prov), _distinct_urls(merged_prov)
            before_conf = target.get("confidence") or 0.0
            after_conf = before_conf
            if after_n > before_n:
                # Monotonic: corroboration never lowers a confidence the extractor
                # or quality-blend set higher than the bare source curve.
                after_conf = max(before_conf, confidence_from_sources(after_n or 1))
                await store.update_finding(
                    ctx.kb_id,
                    d.target_finding_id,
                    confidence=after_conf,
                    provenance=merged_prov,
                )
            events.append(
                ResolutionEvent(
                    op="NOOP",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    details={
                        "merged_urls": sorted(
                            {p.get("url") for p in merged_prov if p.get("url")}
                        ),
                        "confidence_before": before_conf,
                        "confidence_after": after_conf,
                    },
                    reason=d.reason,
                )
            )
        elif d.op in (ResolutionOp.UPDATE, ResolutionOp.SUPERSEDE):
            try:
                target = store.get_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — target vanished → ADD instead
                add_rows.append(_row_from_candidate(ctx, f, emb))
                events.append(
                    ResolutionEvent(
                        op="ADD", candidate_title=f.title,
                        reason=f"{d.op.value.lower()} target missing; added",
                    )
                )
                continue
            row = _row_from_candidate(ctx, f, emb)
            if d.op == ResolutionOp.UPDATE:
                # Refinement inherits the target's sources — same claim, more support.
                row["provenance"] = _merge_provenance(
                    target.get("provenance") or [], normalize_provenance(f.provenance)
                )
            # SUPERSEDE keeps only its own provenance: a contradicted finding's
            # sources must not corroborate the claim that contradicts them.
            row["confidence"] = confidence_from_sources(_distinct_urls(row["provenance"]) or 1)
            try:
                new_id = await store.supersede_finding(ctx.kb_id, d.target_finding_id, row)
            except Exception:  # noqa: BLE001 — atomic: the KB is unchanged → ADD instead
                logger.warning(
                    "resolution %s failed for target %s; falling back to ADD",
                    d.op.value, d.target_finding_id, exc_info=True,
                )
                add_rows.append(row)
                events.append(
                    ResolutionEvent(
                        op="ADD", candidate_title=f.title,
                        reason=(
                            f"{d.op.value.lower()} failed (target retired concurrently?); added"
                        ),
                    )
                )
                continue
            affected.append(new_id)
            events.append(
                ResolutionEvent(
                    op=d.op.value,
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    new_finding_id=new_id,
                    reason=d.reason,
                )
            )

    if add_rows:
        new_ids = await store.insert_findings(add_rows)
        affected.extend(new_ids)
        add_events = [e for e in events if e.op == "ADD"]
        for e, nid in zip(add_events, new_ids):
            e.new_finding_id = nid
    if events:
        await store.insert_resolution_events(ctx.kb_id, [e.model_dump() for e in events])
    return ResolutionOutcome(affected_finding_ids=affected, events=events)
