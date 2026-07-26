"""Deterministic eval-KB builder from pinned public sources.

    manifest.yaml ──► fetch(url) ─► extract_findings ─► Finding ─► resolve_and_persist
                                                   └──► lockfile.json (ids + hashes)
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml

from delapan.core.config import get_config
from delapan.core.exploration.extractor import extract_findings
from delapan.core.exploration.models import Finding
from delapan.core.memory.persist import resolve_and_persist
from delapan.mcp.tenancy import resolve_tenant

LOCKFILE = Path(__file__).parent / "lockfile.json"

_EXTRACTION_PROMPT = (
    "Extract specific, verifiable facts from this page as findings. Each finding: "
    "a concise title and key-value content of concrete facts (versions, dates, "
    "capabilities, numbers). Skip marketing copy."
)


async def _default_fetch(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def build_corpus(
    manifest_path: Path,
    *,
    project: str = "delapan-evals",
    kb: str = "public-v1",
    fetcher: Callable[[str], Awaitable[str]] | None = None,
) -> dict:
    spec = yaml.safe_load(Path(manifest_path).read_text())
    sources = spec.get("sources") or []
    if not sources:
        raise ValueError(f"{manifest_path}: manifest has no sources")
    fetch = fetcher or _default_fetch

    ctx = resolve_tenant(project, kb, create=True)
    from delapan.store import get_store

    store = get_store()
    cfg = get_config()
    exploration_id = uuid.uuid4().hex

    lock_sources: list[dict] = []
    all_ids: list[str] = []
    for src in sources:
        url = src["url"]
        content = await fetch(url)
        result = await extract_findings(content, _EXTRACTION_PROMPT, url, spec["name"],
                                        cfg.exploration)
        candidates = [
            Finding(
                exploration_id=exploration_id, project_id=ctx.project_id, kb=ctx.kb_id,
                category=raw.get("category") or "fact", title=raw["title"],
                content=raw["content"], confidence=float(raw.get("confidence", 0.7)),
                provenance=[{"url": url, "query": spec["name"]}],
            )
            for raw in result.findings
            if raw.get("title") and raw.get("content")
        ]
        outcome = await resolve_and_persist(ctx, store, candidates, cfg)
        lock_sources.append(
            {"url": url, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
             "finding_count": len(outcome.affected_finding_ids)}
        )
        all_ids.extend(outcome.affected_finding_ids)

    lock = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
        "sources": lock_sources,
        "finding_ids": sorted(all_ids),
    }
    LOCKFILE.write_text(json.dumps(lock, indent=2, sort_keys=True))
    return lock
