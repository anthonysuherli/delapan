"""Explore route — the research pipeline over SSE, mirroring `delapan_explore`.

    POST /api/projects/{p}/kbs/{k}/explore  {"prompt", "max_findings"?}
        │
        ├─► missing keys ──► data: {"phase": "error", "error": ...}     (immediately)
        └─► run_exploration (asyncio task) ──► queue ──► SSE
                data: {"phase": "planning|searching|crawling|extracting|merging", "detail": ...}
                data: {"phase": "completed", "finding_ids": [...], "count": N}   (final)
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
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from delapan.api.deps import resolve_kb_or_404
from delapan.core.agent.state import TenantContext
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import get_config, get_settings
from delapan.core.exploration import run_exploration
from delapan.core.knowledge_graph.builder import schedule_kg_update
from delapan.store import Store

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}")


class ExploreBody(BaseModel):
    prompt: str
    max_findings: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _missing_keys() -> list[str]:
    """Credentials the pipeline needs end-to-end (search + LLM + embeddings)."""
    s = get_settings()
    required = (
        ("TAVILY_API_KEY", s.tavily_api_key),
        ("AI_GATEWAY_API_KEY", s.ai_gateway_api_key),
        ("OPENAI_API_KEY", s.openai_api_key),
    )
    return [name for name, value in required if not value]


def _render_content(content: Any) -> str:
    """Render a finding's free-form ``content`` dict to a markdown body.

    Mirrors ``mcp/server.py::_render_content`` verbatim so the persisted shape is
    identical across the MCP and HTTP explore surfaces."""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return str(content)
    if not content:
        return ""

    if len(content) == 1:
        only = next(iter(content.values()))
        if isinstance(only, str):
            return only

    lines: list[str] = []
    for key, value in content.items():
        label = key.replace("_", " ").title()
        if isinstance(value, (list, dict)):
            lines.append(f"**{label}**:")
            lines.append("```json")
            lines.append(json.dumps(value, indent=2))
            lines.append("```")
        else:
            lines.append(f"**{label}**: {value}")
    return "\n".join(lines)


def _normalize_provenance(provenance: Any) -> list[dict]:
    """Findings carry ``[{url, query}]``; keep that shape, stamp ``accessed_at``."""
    if not provenance:
        return []
    out: list[dict] = []
    for p in provenance:
        if isinstance(p, dict):
            entry = dict(p)
            entry.setdefault("accessed_at", _now_iso())
            out.append(entry)
    return out


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

        ids: list[str] = []
        if captured:
            rows: list[dict] = []
            contents: list[str] = []
            for f in captured:
                rendered = _render_content(f.content)
                rows.append(
                    {
                        "org_id": ctx.org_id,
                        "kb_id": ctx.kb_id,
                        "title": f.title,
                        "content": rendered,
                        "category": f.category,
                        "confidence": (float(f.confidence) if f.confidence is not None else None),
                        "tags": list(f.tags or []),
                        "provenance": _normalize_provenance(f.provenance),
                    }
                )
                contents.append(rendered)
            embeddings = await embed_batch(contents)
            for row, emb in zip(rows, embeddings):
                row["embedding"] = emb
            ids = await store.insert_findings(rows)

        store.update_exploration(
            exp_id, status="completed", completed_at=_now_iso(), finding_ids=ids
        )
        await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise

    return {"finding_ids": ids, "count": len(ids)}


async def _events(ctx: TenantContext, store: Store, body: ExploreBody) -> AsyncIterator[str]:
    missing = _missing_keys()
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
async def explore(project: str, kb: str, body: ExploreBody) -> StreamingResponse:
    ctx, store = resolve_kb_or_404(project, kb)
    return StreamingResponse(_events(ctx, store, body), media_type="text/event-stream")
