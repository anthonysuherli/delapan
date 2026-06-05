"""Embedding client. OpenAI for v0; swap for Voyage later if recall demands."""

from __future__ import annotations

import asyncio
from typing import Sequence

from openai import AsyncOpenAI

from delapan.core.config import get_config, get_settings

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Returns a 1536-dim vector."""
    emb = get_config().embedding
    client = _get_client()
    resp = await client.embeddings.create(
        model=emb.model,
        input=text[: emb.input_char_cap],  # safety cap on token count
    )
    return resp.data[0].embedding


async def embed_batch(texts: Sequence[str]) -> list[list[float]]:
    """Embed multiple texts in a single call (cheaper than N round-trips)."""
    if not texts:
        return []
    emb = get_config().embedding
    client = _get_client()
    resp = await client.embeddings.create(
        model=emb.model,
        input=[t[: emb.input_char_cap] for t in texts],
    )
    # Preserve input order
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


async def embed_with_retry(text: str, retries: int = 2) -> list[float]:
    """Embed with a single retry on transient failures."""
    for attempt in range(retries + 1):
        try:
            return await embed_text(text)
        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError("unreachable")
