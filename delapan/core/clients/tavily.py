"""Tavily client — web search + page-content extraction for exploration.

Thin async wrappers over ``tavily-python`` with retry/backoff. ``search``
returns ranked result dicts; ``extract`` fetches readable page content for a
batch of URLs (replacing a dedicated crawler).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from functools import lru_cache
from typing import Any, Awaitable, Callable, Literal, cast

from delapan.core.config import get_settings

logger = logging.getLogger(__name__)

# Tavily caps URLs per /extract request.
_EXTRACT_BATCH = 20


def _with_retry(max_retries: int, base_delay: float, fallback: Callable[[], Any]):
    """Async exponential-backoff retry. Returns ``fallback()`` once exhausted."""

    def decorator(func: Callable[..., Awaitable[Any]]):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — transport/provider errors
                    last_exc = exc
                    if attempt < max_retries:
                        await asyncio.sleep(base_delay * (2**attempt))
            logger.warning("%s exhausted retries: %s", func.__name__, last_exc)
            return fallback()

        return wrapper

    return decorator


@lru_cache(maxsize=1)
def _client():
    from tavily import AsyncTavilyClient

    return AsyncTavilyClient(api_key=get_settings().tavily_api_key)


@_with_retry(max_retries=3, base_delay=1.0, fallback=list)
async def search(query: str, *, max_results: int, search_depth: str) -> list[dict]:
    """Run one Tavily search; returns the ranked ``results`` list ([] on failure)."""
    resp = await _client().search(
        query=query,
        search_depth=cast(Literal["basic", "advanced"], search_depth),
        max_results=max_results,
    )
    return resp.get("results", [])


async def extract(urls: list[str], *, search_depth: str = "advanced") -> dict[str, str]:
    """Fetch readable content for ``urls``. Returns ``{url: content}`` — URLs that
    fail or return nothing are simply absent. Batched to Tavily's per-call cap."""
    if not urls:
        return {}
    extract_depth = "advanced" if search_depth == "advanced" else "basic"
    out: dict[str, str] = {}
    for i in range(0, len(urls), _EXTRACT_BATCH):
        out.update(await _extract_batch(urls[i : i + _EXTRACT_BATCH], extract_depth))
    return out


@_with_retry(max_retries=2, base_delay=1.0, fallback=dict)
async def _extract_batch(urls: list[str], extract_depth: str) -> dict[str, str]:
    resp = await _client().extract(
        urls=urls,
        extract_depth=cast(Literal["basic", "advanced"], extract_depth),
        format="markdown",
    )
    out: dict[str, str] = {}
    for r in resp.get("results", []):
        url = r.get("url")
        content = r.get("raw_content") or r.get("content") or ""
        if url and content:
            out[url] = content
    return out
