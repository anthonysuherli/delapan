"""Tavily client — web search + page-content extraction for exploration.

Thin async wrappers over ``tavily-python`` with retry/backoff. ``search``
returns ranked result dicts; ``extract`` fetches readable page content for a
batch of URLs (replacing a dedicated crawler). On provider failure (quota, auth,
transport), ``TavilyError`` is raised — never silent fallback to empty.
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


class TavilyError(RuntimeError):
    """Search provider failed after retries (quota, auth, transport).

    Raised instead of returning an empty fallback so callers can mark the run
    failed — a provider outage must not masquerade as an empty web."""


def _with_retry(max_retries: int, base_delay: float, fallback: Callable[[], Any] | None = None):
    """Async exponential-backoff retry. Once exhausted: return ``fallback()`` if
    given, else raise ``TavilyError`` chained to the last provider error."""

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
            if fallback is not None:
                return fallback()
            raise TavilyError(
                f"{func.__name__} failed after {max_retries + 1} attempts: {last_exc}"
            ) from last_exc

        return wrapper

    return decorator


@lru_cache(maxsize=1)
def _client():
    from tavily import AsyncTavilyClient

    return AsyncTavilyClient(api_key=get_settings().tavily_api_key)


@_with_retry(max_retries=3, base_delay=1.0)
async def search(query: str, *, max_results: int, search_depth: str) -> list[dict]:
    """Run one Tavily search; returns the ranked ``results`` list.
    Raises ``TavilyError`` if the provider fails after retries."""
    resp = await _client().search(
        query=query,
        search_depth=cast(Literal["basic", "advanced"], search_depth),
        max_results=max_results,
    )
    return resp.get("results", [])


async def extract(urls: list[str], *, search_depth: str = "advanced") -> dict[str, str]:
    """Fetch readable content for ``urls``. Returns ``{url: content}`` — URLs that
    return nothing are simply absent. Batched to Tavily's per-call cap. A failed
    batch is skipped when other batches succeed; if *every* batch fails the
    provider is down and ``TavilyError`` is raised."""
    if not urls:
        return {}
    extract_depth = "advanced" if search_depth == "advanced" else "basic"
    out: dict[str, str] = {}
    errors: list[TavilyError] = []
    for i in range(0, len(urls), _EXTRACT_BATCH):
        try:
            out.update(await _extract_batch(urls[i : i + _EXTRACT_BATCH], extract_depth))
        except TavilyError as exc:
            errors.append(exc)
    if errors and not out:
        raise errors[0]
    return out


@_with_retry(max_retries=2, base_delay=1.0)
async def _extract_batch(urls: list[str], extract_depth: str) -> dict[str, str]:
    """Fetch content for one batch of URLs. Raises TavilyError on provider failure."""
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
