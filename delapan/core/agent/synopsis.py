"""KB synopsis spine: the always-on preamble's stable layer.

    findings (titles+categories) ─► fast LLM ─► [{topic, gloss}] ─► kb_synopsis

Regen is incremental + fire-and-forget: `maybe_rebuild_synopsis` is awaited as a
detached task after each persist, so chat turns never block on it. Storage is the
synopsis spine behind the Store seam (one current row per KB, upserted on kb_id).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from delapan.core.clients.anthropic import chat_model
from delapan.core.config import SynopsisConfig, get_config
from delapan.store import Store, get_store

logger = logging.getLogger(__name__)


def should_rebuild(live_count: int, row: dict | None, cfg: SynopsisConfig) -> bool:
    if live_count <= 0:
        return False
    if row is None:
        return True
    if live_count - int(row.get("finding_count_at_build", 0)) >= cfg.rebuild_delta:
        return True
    built_at = row.get("built_at")
    if built_at:
        ts = datetime.fromisoformat(str(built_at).replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        if age_h >= cfg.rebuild_max_age_hours:
            return True
    return False


def _build_prompt(findings: list[dict], cfg: SynopsisConfig) -> str:
    lines = [f"- {f.get('title', '')} [{f.get('category', '')}]" for f in findings]
    catalogue = "\n".join(lines)
    return (
        "You are summarizing a knowledge base into a compact orientation spine.\n"
        f"Below are its findings (title [category]).\n\n{catalogue}\n\n"
        f"Produce at most {cfg.max_entries} entries naming the KB's main topics. "
        "Return ONLY JSON: a list of objects with keys `topic` (short noun phrase) "
        "and `gloss` (one sentence on what the KB knows about it)."
    )


def load_synopsis(store: Store, kb_id: str) -> dict | None:
    """Current synopsis row for `kb_id`, or None — thin pass-through to the Store."""
    return store.load_synopsis(kb_id)


async def _build(findings: list[dict], cfg: SynopsisConfig) -> list[dict]:
    llm = chat_model(cfg.model)
    resp = await llm.ainvoke([{"role": "user", "content": _build_prompt(findings, cfg)}])
    text = resp.content if isinstance(resp.content, str) else ""
    try:
        data = json.loads(text[text.find("[") : text.rfind("]") + 1])
        # Load-bearing: the dict-filter keeps the [:max_entries] slice safe — a
        # mis-sliced non-dict list (e.g. a parsed JSON object) degrades to [].
        return [e for e in data if isinstance(e, dict)][: cfg.max_entries]
    except (ValueError, json.JSONDecodeError):
        logger.warning("synopsis JSON parse failed; len=%d", len(text))
        return []


async def maybe_rebuild_synopsis(
    kb_id: str, *, org_id: str | None = None, store: Store | None = None
) -> None:
    """Fire-and-forget: rebuild the synopsis if the KB grew enough. Never raises.

    `org_id` is accepted for signature parity with cloud callers but is no longer
    threaded through persistence (the Store owns org scoping)."""
    try:
        cfg = get_config().synopsis
        store = store or get_store()
        live_count = store.count_findings(kb_id)
        row = store.load_synopsis(kb_id)
        if not should_rebuild(live_count, row, cfg):
            return
        listing = store.list_findings(kb_id, limit=200)
        rows = listing.get("findings", []) if isinstance(listing, dict) else []
        findings = [f for f in rows if isinstance(f, dict)]
        content = await _build(findings, cfg)
        store.upsert_synopsis(kb_id, content=content, finding_count=live_count, model=cfg.model)
    except Exception:  # noqa: BLE001 — regen is best-effort, never breaks a turn
        logger.exception("synopsis rebuild failed for kb=%s", kb_id)


_BG_TASKS: set[asyncio.Task] = set()


def schedule_rebuild(kb_id: str, *, org_id: str | None = None, store: Store | None = None) -> None:
    """Fire-and-forget synopsis regen that won't be GC'd mid-flight.

    Holds a strong ref in a module-level set until the task finishes (CPython's
    event loop only weak-refs tasks, so an unreferenced create_task can vanish)."""
    task = asyncio.create_task(maybe_rebuild_synopsis(kb_id, org_id=org_id, store=store))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
