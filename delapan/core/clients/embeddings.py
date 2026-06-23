"""Embedding client. Routes through the Vercel AI Gateway (OpenAI-compatible)
when ``AI_GATEWAY_API_KEY`` is set, else direct OpenAI."""

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
        # Prefer the AI Gateway (one credential for the whole pipeline, no OpenAI
        # account needed); fall back to direct OpenAI when only OPENAI_API_KEY set.
        if settings.ai_gateway_api_key:
            _client = AsyncOpenAI(
                api_key=settings.ai_gateway_api_key,
                base_url=settings.ai_gateway_base_url,
            )
        else:
            _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Returns a 1536-dim vector."""
    emb = get_config().embedding
    client = _get_client()
    resp = await client.embeddings.create(
        model=emb.model,
        input=text[: emb.input_char_cap],  # safety cap on token count
        dimensions=emb.dim,  # pin output dim (gateway/Gemini default differs from the fixed column)
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
        dimensions=emb.dim,  # pin output dim (gateway/Gemini default differs from the fixed column)
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
