"""Explore route — the research pipeline over SSE, mirroring `delapan_explore`.

    POST /api/projects/{p}/kbs/{k}/explore  {"prompt", "max_findings"?}
        │
        ├─► missing keys ──► data: {"phase": "error", "error": ...}     (immediately)
        └─► run_exploration (asyncio task) ──► queue ──► SSE
                data: {"phase": "planning|searching|crawling|extracting|merging", "detail": ...}
                data: {"phase": "completed", "finding_ids": [...], "count": N, "synopsis": S}  (final)
                data: {"phase": "error", "error": "..."}                         (on failure)

The pipeline + persistence sequence mirrors ``mcp/server.py::delapan_explore``
(exploration row, render→embed→insert, synopsis rebuild, KG update); progress
callbacks bridge to the response through an ``asyncio.Queue``. The engine's own
terminal "completed"/"error: ..." progress phases are not forwarded — the final
event carries them with their payloads instead.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from delapan.api.auth import request_tenancy_creating
from delapan.api.deps import missing_pipeline_keys
from delapan.api.ratelimit import limiter, pipeline_limit
from delapan.core.agent.state import TenantContext
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.config import get_config
from delapan.core.exploration import run_exploration
from delapan.core.knowledge_graph.builder import schedule_kg_update
from delapan.core.memory.persist import resolve_and_persist
from delapan.store import Store

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}")


class ExploreBody(BaseModel):
    prompt: str
    max_findings: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _run_and_persist(
    ctx: TenantContext,
    store: Store,
    body: ExploreBody,
    on_progress,
) -> dict:
    """Pipeline + persistence, mirroring ``mcp/server.py::delapan_explore``."""
    cfg = get_config().exploration
    cap = min(body.max_findings or cfg.default_max_findings, cfg.max_findings)

    exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, body.prompt)
    try:
        findings = await run_exploration(
            body.prompt,
            exploration_id=exp_id,
            project_id=ctx.project_id,
            kb_id=ctx.kb_id,
            cfg=cfg,
            on_progress=on_progress,
        )
        captured = findings[:cap]

        outcome = await resolve_and_persist(ctx, store, captured, get_config())
        ids = outcome.affected_finding_ids

        store.update_exploration(
            exp_id, status="completed", completed_at=_now_iso(), finding_ids=ids
        )
        syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise

    return {"finding_ids": ids, "count": len(ids), "synopsis": syn_status}


async def _events(ctx: TenantContext, store: Store, body: ExploreBody) -> AsyncIterator[str]:
    missing = missing_pipeline_keys()
    if missing:
        yield _sse({"phase": "error", "error": f"missing required keys: {', '.join(missing)}"})
        return

    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_progress(phase: str) -> None:
        # The engine's terminal phases are folded into the final event below.
        if phase == "completed" or phase.startswith("error"):
            return
        await queue.put({"phase": phase, "detail": None})

    async def run() -> None:
        try:
            result = await _run_and_persist(ctx, store, body, on_progress)
            await queue.put({"phase": "completed", **result})
        except Exception as exc:  # noqa: BLE001 — surface as a terminal SSE event
            await queue.put({"phase": "error", "error": str(exc)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _sse(item)
    finally:
        if not task.done():
            task.cancel()


@router.post("/explore")
@limiter.limit(pipeline_limit, override_defaults=False)
async def explore(
    request: Request,
    body: ExploreBody,
    tenancy: tuple[TenantContext, Store] = Depends(request_tenancy_creating),
) -> StreamingResponse:
    ctx, store = tenancy
    return StreamingResponse(_events(ctx, store, body), media_type="text/event-stream")
