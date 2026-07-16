"""Memory resolver — decide ADD/UPDATE/NOOP/DELETE per candidate finding.

    candidates + neighbors ─► one LLM pass ─► [ResolutionDecision…]

Ported pattern from mem0's update-memory step (no mem0 dependency): for each new
candidate, retrieve the top-k semantically similar EXISTING findings and let the
model decide whether it is new (ADD), refines one (UPDATE), duplicates one (NOOP),
or contradicts one (DELETE). Tier-agnostic: uses only the Store contract.

Neighbor bodies are unreliable on the local tier (explore-persisted findings read
back with content={}), so the prompt leans on neighbor TITLES + similarity;
candidate bodies are always real. Any failure defaults to ADD — a resolver
failure must never drop a finding.
"""

from __future__ import annotations

import logging

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import MemoryConfig
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import render_content
from delapan.core.memory.models import ResolutionBatch, ResolutionDecision, ResolutionOp
from delapan.store import Store

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You maintain a knowledge base of findings. For each new CANDIDATE finding you "
    "are shown its most semantically-similar EXISTING findings (id, title, similarity). "
    "Decide ONE operation per candidate:\n"
    "- ADD: genuinely new information (no existing finding covers it).\n"
    "- UPDATE: refines/supersedes one existing finding (set target_finding_id to its id).\n"
    "- NOOP: duplicates one existing finding, no new info (set target_finding_id to its id).\n"
    "- DELETE: directly contradicts and invalidates one existing finding "
    "(set target_finding_id to its id). Use sparingly.\n"
    "Return one decision per candidate using the candidate_index you were given. "
    "When unsure, prefer ADD. Only UPDATE/NOOP/DELETE when a neighbor is clearly the same topic."
)


def _candidate_block(i: int, f: Finding, neighbors: list[dict]) -> str:
    body = render_content(f.content)[:600]
    lines = [f"### candidate_index={i}", f"title: {f.title}", f"body: {body}", "neighbors:"]
    if not neighbors:
        lines.append("  (none)")
    for n in neighbors:
        sim = round(float(n.get("similarity", 0.0)), 3)
        lines.append(f"  - id={n.get('id')} sim={sim} title={n.get('title')!r}")
    return "\n".join(lines)


async def resolve(
    store: Store,
    kb_id: str,
    candidates: list[Finding],
    embeddings: list[list[float]],
    cfg: MemoryConfig,
) -> list[ResolutionDecision]:
    """One decision per candidate (same order + length as ``candidates``).

    Candidates with no neighbor above the similarity floor short-circuit to ADD
    without consuming an LLM slot. need-LLM candidates are chunked by
    ``cfg.max_candidates_per_pass`` (one LLM call per chunk); any chunk failure →
    those candidates stay ADD. The ``cfg.enabled`` kill-switch is enforced upstream
    in resolve_and_persist, not here."""
    if not candidates:
        return []

    neighbor_sets: list[list[dict]] = []
    for emb in embeddings:
        try:
            hits = await store.match_findings(
                kb_id,
                emb,
                match_count=cfg.neighbor_top_k,
                min_similarity=cfg.neighbor_min_similarity,
            )
        except Exception:  # noqa: BLE001 — retrieval failure → treat as no neighbors
            hits = []
        neighbor_sets.append(hits)

    # Default everything to ADD; only candidates with neighbors need the LLM.
    decisions: list[ResolutionDecision] = [
        ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD, reason="no similar finding")
        for i in range(len(candidates))
    ]
    need_llm = [i for i, ns in enumerate(neighbor_sets) if ns]
    if not need_llm:
        return decisions

    # Chunk to honor the per-pass cap; each chunk is one LLM call. A chunk that
    # fails leaves its candidates as the pre-initialized ADD (never dropped).
    cap = max(cfg.max_candidates_per_pass, 1)
    for start in range(0, len(need_llm), cap):
        chunk = need_llm[start : start + cap]
        try:
            user = "\n\n".join(_candidate_block(i, candidates[i], neighbor_sets[i]) for i in chunk)
            batch = await structured_completion(
                model=cfg.resolution_model,
                response_format=ResolutionBatch,
                system=_SYSTEM,
                user=user,
                temperature=cfg.temperature,
                fallback_model=cfg.resolution_fallback_model,
                reasoning_effort=cfg.reasoning_effort,
                use_json_schema=True,
            )
        except Exception as exc:  # noqa: BLE001 — never drop findings; this chunk stays ADD
            logger.warning("memory resolution failed (%s); ADD-all for this chunk", exc)
            continue

        valid = set(chunk)
        seen: set[int] = set()
        for d in batch.decisions:
            i = d.candidate_index
            if i not in valid or i in seen:
                continue  # ignore out-of-chunk indices and duplicate decisions
            seen.add(i)
            if d.op in (ResolutionOp.UPDATE, ResolutionOp.NOOP, ResolutionOp.DELETE):
                neighbor_ids = {n.get("id") for n in neighbor_sets[i]}
                if d.target_finding_id is None or d.target_finding_id not in neighbor_ids:
                    # Hallucinated, empty, or out-of-scope target — demote to a safe ADD.
                    decisions[i] = ResolutionDecision(
                        candidate_index=i,
                        op=ResolutionOp.ADD,
                        reason="model named unknown/empty target; demoted to ADD",
                    )
                    continue
            decisions[i] = d
    return decisions
