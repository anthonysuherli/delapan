"""Finding extractor.

Ported from delapan's `features/exploration/extractor.py`. Sends crawled page
content + the planner's extraction prompt to the extraction model via AI Gateway
and returns structured findings. Keeps the anti-template guardrails verbatim —
they are what stop the model returning `[placeholder]` titles / empty content.
"""

from __future__ import annotations

import logging

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import ExplorationConfig
from delapan.core.exploration.models import ExtractionResult, FindingBatch

logger = logging.getLogger(__name__)

_GUARDRAILS = (
    "\n\nCRITICAL CONSTRAINTS:\n"
    "1. EXTRACT ONLY ACTUAL CONTENT from the provided text. Do NOT return template "
    "placeholders like [word], {word}, or {placeholder}.\n"
    "2. Every finding must have a concrete 'title' with real information extracted from the source.\n"
    "3. Every finding must have a non-empty 'content' dict with actual extracted data.\n"
    "4. If you cannot extract real content, return an empty list rather than template placeholders.\n"
    "5. Return findings as a list. Each item MUST have 'title' (string with real data), "
    "'content' (non-empty dict), and 'category' (string)."
)


async def extract_findings(
    content: str,
    extraction_prompt: str,
    source_url: str,
    source_query: str,
    cfg: ExplorationConfig,
) -> ExtractionResult:
    """Extract findings from one page's content. Never raises — failures yield
    an empty `ExtractionResult` so the engine's gather isn't poisoned."""
    truncated = content[: cfg.max_content_per_page]
    system = f"{extraction_prompt}{_GUARDRAILS}"

    try:
        batch: FindingBatch = await structured_completion(
            model=cfg.extraction_model,
            response_format=FindingBatch,
            system=system,
            user=truncated,
            temperature=cfg.temperature,
            fallback_model=cfg.extraction_fallback_model,
            reasoning_effort=cfg.reasoning_effort,
            # Findings carry free-form `content` dicts; strict json_schema would
            # force those empty, so instruct the schema in the prompt instead.
            use_json_schema=False,
        )
        findings = [f.model_dump(exclude_none=True) for f in batch.findings]
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort per page
        logger.warning("extraction failed for %s: %s", source_url, exc)
        findings = []

    return ExtractionResult(
        findings=findings,
        source_url=source_url,
        source_query=source_query,
        model_used=cfg.extraction_model,
    )
