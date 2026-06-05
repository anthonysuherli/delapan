"""Exploration orchestrator: plan → search → crawl → extract → merge.

Ported from delapan's `ExplorationEngine._run_pipeline`, stripped of narration,
telemetry, the `Store` abstraction, and ontology (v0 has none). Pure of Supabase
and SSE: takes a prompt + config + progress callback, returns `list[Finding]`.
`tools/explore.py` owns the `explorations` row and finding persistence.

Progress phases emitted via `on_progress`, in order:
    planning → searching → crawling → extracting → merging → completed
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlparse

from delapan.core.clients import tavily
from delapan.core.config import ExplorationConfig, get_config
from delapan.core.exploration.evaluator import evaluate_findings
from delapan.core.exploration.extractor import extract_findings
from delapan.core.exploration.merger import FindingMerger
from delapan.core.exploration.models import (
    ExplorationPlan,
    Finding,
    SearchQuery,
    Source,
)
from delapan.core.exploration.narrator import narrate
from delapan.core.exploration.planner import plan_queries

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str], Awaitable[None]]
NarrationCb = Callable[[str, str], Awaitable[None]]

_T = TypeVar("_T")


async def _narrated(
    phase: str,
    work: Awaitable[_T],
    ctx: dict[str, object],
    *,
    on_narration: NarrationCb | None,
    ncfg,
) -> _T:
    """Run a phase's real work and a best-effort narration concurrently — the
    narration LLM call adds no latency (gather); emit it via `on_narration` if any
    text comes back."""
    if not on_narration or not ncfg.enabled:
        return await work
    result, text = await asyncio.gather(work, narrate(phase, ctx, ncfg))
    if text:
        await on_narration(phase, text)
    return result


async def run_exploration(
    prompt: str,
    *,
    exploration_id: str,
    project_id: str,
    kb_id: str,
    cfg: ExplorationConfig,
    on_progress: ProgressCb | None = None,
    on_narration: NarrationCb | None = None,
    lens: str = "explore",
) -> list[Finding]:
    """Run the full pipeline for `prompt`; return merged findings (unpersisted)."""

    async def progress(phase: str) -> None:
        if on_progress:
            await on_progress(phase)

    ncfg = get_config().narration

    try:
        # Phase 1 — plan
        await progress("planning")
        plan = await _narrated(
            "planning",
            plan_queries(prompt, cfg, lens=lens),
            {"prompt": prompt[:120]},
            on_narration=on_narration,
            ncfg=ncfg,
        )

        # Phase 2 — search
        await progress("searching")
        search_results = await _narrated(
            "searching",
            _run_search(plan.search_queries, cfg),
            {"queries": [q.query for q in plan.search_queries]},
            on_narration=on_narration,
            ncfg=ncfg,
        )
        if not search_results:
            await progress("completed")
            return []

        sources, urls, url_to_query = _build_sources(search_results)

        # Phase 3 — crawl (fetch readable content for the top URLs)
        await progress("crawling")
        content_by_url = await tavily.extract(urls[: cfg.max_pages], search_depth=cfg.search_depth)
        for src in sources:
            if content_by_url.get(src.url):
                src.was_crawled = True
                src.crawled_at = datetime.now(timezone.utc)

        # Phases 4–5 — extract, evaluate, merge (shared with the agent path).
        findings = await ingest_pages(
            content_by_url,
            url_to_query,
            plan,
            exploration_id=exploration_id,
            project_id=project_id,
            kb_id=kb_id,
            cfg=cfg,
            on_progress=on_progress,
            on_narration=on_narration,
        )

        await progress("completed")
        return findings

    except Exception as exc:
        await progress(f"error: {exc}")
        raise


async def ingest_pages(
    content_by_url: dict[str, str],
    url_to_query: dict[str, str],
    plan: ExplorationPlan,
    *,
    exploration_id: str,
    project_id: str,
    kb_id: str,
    cfg: ExplorationConfig,
    on_progress: ProgressCb | None = None,
    on_narration: NarrationCb | None = None,
) -> list[Finding]:
    """Keyless back half of the pipeline: extract → evaluate → merge. Shared by
    `run_exploration` (Tavily content) and the agent-handoff path (host-fetched
    content). Pure of Supabase/SSE."""
    ncfg = get_config().narration

    async def progress(phase: str) -> None:
        if on_progress:
            await on_progress(phase)

    await progress("extracting")
    extraction_results = await _narrated(
        "extracting",
        _run_extraction(content_by_url, url_to_query, plan.extraction_prompt, cfg),
        {"page_count": len(content_by_url)},
        on_narration=on_narration,
        ncfg=ncfg,
    )
    findings = _build_findings(extraction_results, plan, exploration_id, project_id, kb_id)
    if findings:
        findings = await evaluate_findings(findings, cfg)

    await progress("merging")
    if findings:
        merger = FindingMerger(
            fuzzy_threshold=cfg.fuzzy_match_threshold,
            min_confidence=cfg.min_confidence_threshold,
        )
        findings = await asyncio.to_thread(merger.merge_findings, findings)

    return findings


# -----------------------------------------------------------------------------
# Search
# -----------------------------------------------------------------------------


async def _run_search(search_queries: list[SearchQuery], cfg: ExplorationConfig) -> list[dict]:
    """Run planned queries by priority tier, deduping URLs. Queries *within* a
    tier fan out concurrently (the orchestrator-worker "map"); tiers stay ordered
    so a higher-priority tier can still be skipped once enough results accumulate.
    Concurrency is bounded by `max_concurrent_searches`."""
    by_priority: dict[int, list[SearchQuery]] = {}
    for sq in search_queries:
        by_priority.setdefault(sq.priority, []).append(sq)

    seen_urls: set[str] = set()
    all_results: list[dict] = []
    sem = asyncio.Semaphore(cfg.max_concurrent_searches)

    async def _one(sq: SearchQuery) -> tuple[SearchQuery, list[dict]]:
        query_str = f"{sq.query} {sq.domain_filter}".strip() if sq.domain_filter else sq.query
        async with sem:
            results = await tavily.search(
                query_str,
                max_results=cfg.max_results_per_query,
                search_depth=cfg.search_depth,
            )
        return sq, results

    for priority in sorted(by_priority):
        if priority == 2 and len(all_results) >= cfg.fallback_result_threshold:
            continue
        if priority == 3 and len(all_results) >= cfg.expansion_result_threshold:
            continue

        tier = await asyncio.gather(*[_one(sq) for sq in by_priority[priority]])
        # Merge in planned order so dedup is deterministic regardless of which
        # concurrent search returned first.
        for sq, results in tier:
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    r["_source_query"] = sq.query
                    all_results.append(r)

    return all_results


def _build_sources(
    search_results: list[dict],
) -> tuple[list[Source], list[str], dict[str, str]]:
    sources: list[Source] = []
    urls: list[str] = []
    url_to_query: dict[str, str] = {}
    for r in search_results:
        url = r.get("url", "")
        if not url:
            continue
        urls.append(url)
        query = r.get("_source_query", "")
        url_to_query[url] = query
        sources.append(
            Source(
                url=url,
                title=r.get("title"),
                domain=urlparse(url).netloc,
                search_query=query,
            )
        )
    return sources, urls, url_to_query


# -----------------------------------------------------------------------------
# Extraction
# -----------------------------------------------------------------------------


async def _run_extraction(
    content_by_url: dict[str, str],
    url_to_query: dict[str, str],
    extraction_prompt: str,
    cfg: ExplorationConfig,
) -> list:
    """Extract findings from each crawled page, bounded by a concurrency cap."""
    sem = asyncio.Semaphore(cfg.max_concurrent_extractions)

    async def _one(url: str, content: str):
        async with sem:
            return await extract_findings(
                content=content,
                extraction_prompt=extraction_prompt,
                source_url=url,
                source_query=url_to_query.get(url, ""),
                cfg=cfg,
            )

    tasks = [_one(url, content) for url, content in content_by_url.items() if content]
    return await asyncio.gather(*tasks, return_exceptions=True)


def _build_findings(
    extraction_results: list,
    plan: ExplorationPlan,
    exploration_id: str,
    project_id: str,
    kb_id: str,
) -> list[Finding]:
    """Convert raw extracted dicts into `Finding` objects, skipping template
    placeholders and empty content. Uses the structured `RawFinding` fields
    directly (title/content/category) rather than delapan's whole-dict heuristic."""
    default_category = plan.expected_categories[0] if plan.expected_categories else "general"
    findings: list[Finding] = []

    for result in extraction_results:
        if isinstance(result, Exception):
            continue
        for raw in result.findings:
            if not isinstance(raw, dict):
                continue
            content = raw.get("content") or {}
            title = raw.get("title") or _build_title(content, plan.finding_title_hint)

            is_empty = not content or all(not v for v in content.values())
            if _is_template_placeholder(title) or is_empty:
                logger.debug("skipping template/empty finding: %r", title)
                continue

            relationships = raw.get("relationships")
            if isinstance(relationships, str):
                relationships = [{"description": relationships}]

            findings.append(
                Finding(
                    exploration_id=exploration_id,
                    project_id=project_id,
                    kb=kb_id,
                    category=raw.get("category") or default_category,
                    title=title,
                    content=content,
                    confidence=float(raw.get("confidence", 0.7)),
                    provenance=[{"url": result.source_url, "query": result.source_query}],
                    extraction_model=result.model_used,
                    entity_type=raw.get("entity_type"),
                    relationships=relationships,
                    layout_hint=raw.get("layout_hint"),
                )
            )

    return findings


# -----------------------------------------------------------------------------
# Title / placeholder helpers (ported verbatim from delapan engine)
# -----------------------------------------------------------------------------


def _is_template_placeholder(title: str) -> bool:
    """Detect template-style titles like `[name]` or `{value}`."""
    if not title or not isinstance(title, str):
        return False
    has_brackets = "[" in title and "]" in title
    has_braces = "{" in title and "}" in title
    bracket_count = title.count("[") + title.count("]") + title.count("{") + title.count("}")
    return (has_brackets or has_braces) and bracket_count > 0 and len(title) < 100


def _build_title(content: dict, hint: str) -> str:
    try:
        title = hint.format(**content)
        if "{" in title:
            for v in content.values():
                if isinstance(v, str) and v:
                    return v
            return str(content)
        return title
    except (KeyError, IndexError, TypeError):
        for v in content.values():
            if isinstance(v, str) and v:
                return v
        return str(content)
