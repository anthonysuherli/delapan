"""Verdict recording — the flywheel's write side, off the hot path.

    (query, verdict, bands, qvec) ─► record_access          (ground truth)
                                  ├─► gap/sparse ─► topic upsert | bump
                                  ├─► rich       ─► resolve matching topic
                                  └─► sampled prune

Every transition rule lives here, once — the stores stay CRUD-dumb, so the two
tiers cannot drift. Nothing in this module may raise into a caller: recording is
best-effort by contract (`Store.record_access`), and the hot path must be
byte-identical with curation on or off.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from delapan.core.config import CurationConfig, get_config
from delapan.store import Store

logger = logging.getLogger(__name__)

_BG_TASKS: set[asyncio.Task] = set()


def normalize_query(q: str) -> str:
    """Casefold + collapse whitespace — the exact-match key for `(kb_id, query_norm)`."""
    return " ".join((q or "").lower().split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def schedule_record(
    store: Store,
    *,
    kb_id: str,
    org_id: str,
    surface: str,
    query: str | None,
    coverage: str,
    bands: dict[int, list[dict]],
    embedding: list[float] | None,
) -> None:
    """Fire-and-forget verdict recording that won't be GC'd mid-flight.

    Holds a strong ref in a module-level set until the task finishes (CPython's
    event loop only weak-refs tasks, so an unreferenced create_task can vanish).
    Returns immediately: the caller never waits on the store."""
    cfg = get_config().curation
    if not cfg.enabled or not query:
        return
    try:
        task = asyncio.create_task(
            _record(
                store,
                kb_id=kb_id,
                org_id=org_id,
                surface=surface,
                query=query,
                coverage=coverage,
                bands=bands,
                embedding=embedding,
                cfg=cfg,
            )
        )
    except RuntimeError:  # no running loop (sync caller) — recording is optional
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _record(
    store: Store,
    *,
    kb_id: str,
    org_id: str,
    surface: str,
    query: str,
    coverage: str,
    bands: dict[int, list[dict]],
    embedding: list[float] | None,
    cfg: CurationConfig,
) -> None:
    """Persist one verdict and advance its topic. Never raises."""
    try:
        if not cfg.enabled or len((query or "").strip()) < cfg.min_query_chars:
            return

        band_counts = {str(b): len(rows) for b, rows in (bands or {}).items()}
        await store.record_access(
            org_id=org_id,
            kb_id=kb_id,
            surface=surface,
            targets=[],
            query_text=query,
            coverage=coverage,
            band_counts=band_counts,
        )

        if embedding:
            await _advance_topic(
                store,
                kb_id=kb_id,
                org_id=org_id,
                query=query,
                coverage=coverage,
                embedding=embedding,
                cfg=cfg,
            )

        if random.random() < cfg.prune_sample_rate:  # noqa: S311 — sampling, not crypto
            horizon = (_now() - timedelta(days=cfg.events_retention_days)).isoformat()
            await store.prune_access_events(kb_id, horizon)
    except Exception:  # noqa: BLE001 — recording must never break the caller
        logger.debug("curation: recording failed for kb=%s", kb_id, exc_info=True)


async def _advance_topic(
    store: Store,
    *,
    kb_id: str,
    org_id: str,
    query: str,
    coverage: str,
    embedding: list[float],
    cfg: CurationConfig,
) -> None:
    """Assign this query to a topic and apply the coverage transition.

    `rich` resolves a matching topic (the gap closed); `gap`/`sparse` bumps the
    match or inserts a new topic. The insert is an atomic upsert on
    `(kb_id, query_norm)`, so a concurrent identical recording increments rather
    than double-inserting."""
    now = _now().isoformat()
    hits = await store.match_curation_topics(kb_id, embedding, 1, cfg.topic_match_threshold)
    hit = hits[0] if hits else None
    # Belt-and-suspenders: both tiers already filter server-side on `min_similarity`,
    # but re-check here so a store that returns an unfiltered nearest-neighbor can't
    # silently misclassify a paraphrase match.
    if hit is not None and hit.get("similarity", 0.0) < cfg.topic_match_threshold:
        hit = None

    if coverage == "rich":
        if hit:  # coverage flipped → the topic is done
            await store.update_curation_topic(kb_id, hit["id"], resolved_at=now)
        return

    if hit:
        await store.bump_curation_topic(kb_id, hit["id"], coverage=coverage, seen_at=now)
        return

    await store.upsert_curation_topic(
        {
            "org_id": org_id,
            "kb_id": kb_id,
            "query_text": query,
            "query_norm": normalize_query(query),
            "embedding": embedding,
            "coverage": coverage,
            "seen_at": now,
        }
    )
