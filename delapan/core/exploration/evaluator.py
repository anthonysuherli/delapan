"""Reflection critic over extracted findings (evaluator-optimizer pattern).

    findings ─► one batched critic call ─► quality score + keep verdict ─► findings'

Extraction is the "generate" step; this is the "evaluate" step. A single batched
LLM call scores every finding's signal quality (specificity, factual density,
relevance) in [0,1] and flags vacuous ones for dropping. The score is folded into
the merger's confidence so a thin single-source finding ranks below a dense one —
turning confidence into a *quality* signal, not just a source count.

Best-effort: any failure (provider down, malformed verdicts) leaves the findings
untouched (quality stays 1.0, nothing dropped) so the pipeline is never poisoned.
Gated by `cfg.enable_evaluation`; batched (one call per pipeline run), so the cost
is a single critic pass, not one call per finding.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import ExplorationConfig
from delapan.core.exploration.models import Finding

logger = logging.getLogger(__name__)

_CRITIC_SYSTEM = (
    "You are a strict research critic. You are given numbered findings extracted "
    "from web pages. For EACH finding, judge its signal quality as a research fact:\n"
    "- quality (0.0-1.0): high = specific, factual, quantified, on-topic; "
    "low = vague, generic, boilerplate, navigational, or duplicative filler.\n"
    "- keep: false ONLY if the finding is essentially a non-finding (empty of real "
    "information, a site artifact, or pure marketing fluff).\n"
    "Return one verdict per finding, echoing its index. Be calibrated: reserve "
    "quality > 0.8 for genuinely dense, specific findings."
)

_SNIPPET_CHARS = 600


class _FindingVerdict(BaseModel):
    index: int = Field(description="The 0-based index of the finding being judged")
    quality: float = Field(default=1.0, ge=0.0, le=1.0)
    keep: bool = True
    reason: str = ""


class _EvaluationBatch(BaseModel):
    verdicts: list[_FindingVerdict] = Field(default_factory=list)


def _catalogue(findings: list[Finding]) -> str:
    lines: list[str] = []
    for i, f in enumerate(findings):
        body = " ".join(f"{k}: {v}" for k, v in f.content.items())[:_SNIPPET_CHARS]
        lines.append(f"[{i}] {f.title}\n{body}")
    return "\n\n".join(lines)


async def evaluate_findings(findings: list[Finding], cfg: ExplorationConfig) -> list[Finding]:
    """Score findings' signal quality and drop non-findings. Returns the surviving
    findings with `quality` set; on any failure returns the input unchanged."""
    if not cfg.enable_evaluation or not findings:
        return findings

    try:
        batch: _EvaluationBatch = await structured_completion(
            model=cfg.evaluation_model,
            response_format=_EvaluationBatch,
            system=_CRITIC_SYSTEM,
            user=_catalogue(findings),
            temperature=cfg.temperature,
            fallback_model=cfg.extraction_fallback_model,
            reasoning_effort=cfg.reasoning_effort,
        )
    except Exception as exc:  # noqa: BLE001 — evaluation is best-effort, never fatal
        logger.warning("finding evaluation failed; keeping all findings: %s", exc)
        return findings

    verdicts = {v.index: v for v in batch.verdicts}
    kept: list[Finding] = []
    for i, f in enumerate(findings):
        v = verdicts.get(i)
        if v is None:  # critic skipped this one → keep, unpenalized
            kept.append(f)
            continue
        if not v.keep:
            logger.debug("evaluator dropped finding %r: %s", f.title, v.reason)
            continue
        kept.append(f.model_copy(update={"quality": v.quality}))
    return kept
