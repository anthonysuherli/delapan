"""Per-phase narration — a cheap LLM line describing what the pipeline is doing.

    phase + context ─► one gateway call ─► short prose (best-effort, "" on failure)

Ported from delapan's `features/exploration/narrator.py`, stripped to a single
stateless call. The engine runs this CONCURRENTLY with each phase's real work
(asyncio.gather), so it adds no latency; any failure yields "" and is swallowed.
"""

from __future__ import annotations

import logging

from delapan.core.clients.ai_gateway import text_completion
from delapan.core.config import NarrationConfig

logger = logging.getLogger(__name__)

_SYSTEM = """\
You narrate a live web-research pipeline to a waiting user. Given the current \
phase and a little context, write ONE short present-tense sentence (max 14 words) \
describing what's happening. No preamble, no markdown, just the sentence."""

_PHASE_HINT = {
    "planning": "Deciding what to search for.",
    "searching": "Running web searches.",
    "crawling": "Fetching source pages.",
    "extracting": "Pulling structured findings from pages.",
    "merging": "Deduplicating and scoring findings.",
}


async def narrate(phase: str, context: dict[str, object], cfg: NarrationConfig) -> str:
    """Return a one-line narration for `phase`. Never raises; returns "" on error
    or when disabled."""
    if not cfg.enabled:
        return ""
    user = f"phase: {phase}\nhint: {_PHASE_HINT.get(phase, '')}\ncontext: {context}"
    try:
        text = await text_completion(
            model=cfg.model,
            system=_SYSTEM,
            user=user,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        return (text or "").strip()
    except Exception:  # noqa: BLE001 — narration is best-effort, never breaks a run
        logger.debug("narration failed for phase %s", phase, exc_info=True)
        return ""
