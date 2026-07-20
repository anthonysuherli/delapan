"""Canvas routes — ephemeral search + keep-gated persistence.

    POST /api/projects/{p}/kbs/{k}/canvas/search   {"prompt", "history"?, "max_candidates"?}
        │  SSE: grounding → planning…merging → candidates → answer* → completed|error
        │  (runs the pipeline with NO persistence — candidates are ephemeral)
    POST /api/projects/{p}/kbs/{k}/canvas/keep     {"candidates": [...]}
        │  persists kept candidates through the memory resolver
        └─► {"finding_ids", "events", "synopsis"}

Search is browse-only; `/keep` is the explicit HITL persistence gate, routed
through ``resolve_and_persist`` (ADD/UPDATE/NOOP/SUPERSEDE) — its events are
the frontend's graph-delta + character feed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from delapan.api.auth import request_tenancy
from delapan.api.deps import missing_pipeline_keys
from delapan.api.ratelimit import _local_tier_exempt, limiter, pipeline_limit
from delapan.core.agent.preamble import select_preamble
from delapan.core.agent.state import TenantContext
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.canvas.answer import stream_answer
from delapan.core.config import get_config
from delapan.core.exploration import run_exploration
from delapan.core.exploration.models import Finding
from delapan.core.knowledge_graph.builder import schedule_kg_update
from delapan.core.memory.persist import resolve_and_persist
from delapan.store import Store

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}")


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CanvasSearchBody(BaseModel):
    prompt: str
    history: list[ChatTurn] = Field(default_factory=list)
    max_candidates: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


_CANDIDATE_FIELDS = {"id", "category", "title", "content", "confidence", "tags", "provenance"}


async def _search_events(
    ctx: TenantContext, store: Store, body: CanvasSearchBody
) -> AsyncIterator[str]:
    missing = missing_pipeline_keys()
    if missing:
        yield _sse({"phase": "error", "error": f"missing required keys: {', '.join(missing)}"})
        return

    cfg = get_config()
    ccfg = cfg.canvas
    cap = min(body.max_candidates or ccfg.max_candidates, ccfg.max_candidates)

    try:
        preamble_xml, coverage = await select_preamble(
            body.prompt, store=store, kb_id=ctx.kb_id, depth="shallow"
        )
    except Exception as exc:  # noqa: BLE001 — embedding/provider failure; surface as SSE error
        yield _sse({"phase": "error", "error": f"grounding failed: {exc}"})
        return
    yield _sse({"phase": "grounding", "coverage": coverage})

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    result: dict = {}

    async def on_progress(phase: str) -> None:
        if phase == "completed" or phase.startswith("error"):
            return
        await queue.put({"phase": phase, "detail": None})

    async def run() -> None:
        exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, body.prompt)
        try:
            findings = await run_exploration(
                body.prompt,
                exploration_id=exp_id,
                project_id=ctx.project_id,
                kb_id=ctx.kb_id,
                cfg=cfg.exploration,
                on_progress=on_progress,
            )
            # Browse-only: record the run, persist nothing.
            store.update_exploration(
                exp_id, status="completed", completed_at=_now_iso(), finding_ids=[]
            )
            result["exploration_id"] = exp_id
            result["candidates"] = findings[:cap]
        except Exception as exc:  # noqa: BLE001 — mark the row failed, surface as SSE error
            store.update_exploration(
                exp_id, status="failed", completed_at=_now_iso(), error=str(exc)
            )
            result["error"] = str(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _sse(item)

        if "error" in result:
            yield _sse({"phase": "error", "error": result["error"]})
            return

        candidates: list[Finding] = result["candidates"]
        yield _sse(
            {
                "phase": "candidates",
                "exploration_id": result["exploration_id"],
                "candidates": [f.model_dump(include=_CANDIDATE_FIELDS) for f in candidates],
            }
        )

        try:
            async for delta in stream_answer(
                body.prompt,
                preamble_xml=preamble_xml,
                candidates=candidates,
                history=[t.model_dump() for t in body.history],
                cfg=ccfg,
            ):
                yield _sse({"phase": "answer", "delta": delta})
        except Exception as exc:  # noqa: BLE001 — candidates already delivered; surface and stop
            yield _sse({"phase": "error", "error": f"answer synthesis failed: {exc}"})
            return

        yield _sse({"phase": "completed", "candidate_count": len(candidates)})
    finally:
        if not task.done():
            task.cancel()


@router.post("/canvas/search")
@limiter.limit(pipeline_limit, override_defaults=False, exempt_when=_local_tier_exempt)
async def canvas_search(
    request: Request,
    body: CanvasSearchBody,
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy),
) -> StreamingResponse:
    ctx, store = tenancy
    return StreamingResponse(_search_events(ctx, store, body), media_type="text/event-stream")


class CandidateIn(BaseModel):
    category: str = ""
    title: str
    content: dict
    confidence: float = 0.0
    tags: list[str] = Field(default_factory=list)
    provenance: list[dict] = Field(default_factory=list)


class KeepBody(BaseModel):
    candidates: list[CandidateIn]


def _clamped_content(content: dict, cap: int) -> dict:
    return {k: (v[:cap] if isinstance(v, str) else v) for k, v in content.items()}


@router.post("/canvas/keep")
@limiter.limit(pipeline_limit, override_defaults=False, exempt_when=_local_tier_exempt)
async def canvas_keep(
    request: Request,
    response: Response,  # unused directly — slowapi injects rate-limit headers
    # onto it since this endpoint returns a plain dict, not a Response instance
    body: KeepBody,
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy),
) -> dict:
    """Persist kept candidates through the memory resolver (the HITL gate)."""
    ctx, store = tenancy
    if not body.candidates:
        raise HTTPException(status_code=400, detail="no candidates to keep")

    cfg = get_config()
    ccfg = cfg.canvas
    kept = body.candidates[: ccfg.keep_max_candidates]
    candidates = [
        Finding(
            exploration_id="canvas-keep",
            project_id=ctx.project_id,
            category=c.category,
            title=c.title,
            content=_clamped_content(c.content, ccfg.keep_max_content_chars),
            confidence=c.confidence,
            tags=c.tags,
            provenance=c.provenance,
        )
        for c in kept
    ]

    outcome = await resolve_and_persist(ctx, store, candidates, cfg)
    ids = outcome.affected_finding_ids
    syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
    schedule_kg_update(ctx, ids, store=store)
    return {
        "finding_ids": ids,
        "events": [e.model_dump() for e in outcome.events],
        "synopsis": syn_status,
    }
