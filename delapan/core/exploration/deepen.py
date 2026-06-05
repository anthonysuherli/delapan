"""Autonomous gap-following deep research — the `deepen` loop.

Pure of Supabase/SSE, like `run_exploration`. Composes `run_exploration` per
facet; Supabase-touching work (persist, coverage probe) is injected as
callbacks so this module stays testable and the exploration/ boundary clean.

    decompose(topic) → facets
        │
        ▼  ┌─ round r ──────────────────────────────────────────┐
        └─▶│ gather(run_exploration(f) for f in facets)          │
           │ merge cross-facet → filter cross-round dupes        │
           │ on_round(fresh)         # tool persists             │
           │ critic → {next_facets, done}                        │
           │ stop if r+1≥min_rounds & (done or coverage_probe)   │
           └─────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import DeepenConfig, ExplorationConfig
from delapan.core.exploration.engine import run_exploration
from delapan.core.exploration.merger import FindingMerger
from delapan.core.exploration.models import Finding

logger = logging.getLogger(__name__)


# -- LLM structured-output schemas -------------------------------------------


class FacetPlan(BaseModel):
    facets: list[str] = Field(default_factory=list)


class CriticVerdict(BaseModel):
    coverage: float = Field(ge=0.0, le=1.0)
    unanswered: list[str] = Field(default_factory=list)
    next_facets: list[str] = Field(default_factory=list)
    done: bool = False


# -- Results ------------------------------------------------------------------


@dataclass
class RoundResult:
    round: int
    facets: list[str]
    findings: list[Finding]
    verdict: CriticVerdict | None = None


@dataclass
class DeepenResult:
    rounds: list[RoundResult] = field(default_factory=list)
    gap_set: list[str] = field(default_factory=list)


# -- Dedupe helper (cross-round; merger handles cross-facet within a round) ---


def _is_duplicate(f: Finding, seen: list[Finding], threshold: float) -> bool:
    """True if `f` fuzzy-matches an already-seen finding in the same category."""
    for s in seen:
        if s.category != f.category:
            continue
        if SequenceMatcher(None, f.title.lower(), s.title.lower()).ratio() >= threshold:
            return True
    return False


# -- LLM steps: decompose + critic --------------------------------------------


_DECOMPOSE_PROMPT = """\
You are decomposing a research topic into 3-5 distinct FACETS for parallel \
investigation. Each facet is a focused sub-question or angle that can be \
researched independently — non-overlapping, concrete, and answerable from web \
sources. Return only the facet strings."""

_CRITIC_PROMPT = """\
You are judging how completely a set of research findings covers a topic, and \
deciding whether to keep researching. Given the topic and the findings gathered \
so far, return:
- coverage: 0.0-1.0, how completely the findings answer the topic
- unanswered: the specific open questions still not addressed
- next_facets: 0-5 focused follow-up facets that would close the biggest gaps \
(empty if coverage is sufficient)
- done: true only if the topic is well-covered and further research has \
diminishing returns
Be strict: prefer another round over declaring done prematurely."""


def _findings_digest(findings: list[Finding], cap: int = 60) -> str:
    """Compact title+category list for the critic prompt (keeps tokens bounded)."""
    lines = [f"- [{f.category}] {f.title}" for f in findings[:cap]]
    if len(findings) > cap:
        lines.append(f"... (+{len(findings) - cap} more)")
    return "\n".join(lines) or "(no findings yet)"


async def decompose_topic(topic: str, cfg: DeepenConfig) -> list[str]:
    """One structured call: topic → 3-5 facet prompts."""
    plan = await structured_completion(
        model=cfg.decompose_model,
        response_format=FacetPlan,
        system=_DECOMPOSE_PROMPT,
        user=topic,
        temperature=cfg.temperature,
        reasoning_effort=cfg.reasoning_effort,
    )
    return plan.facets


async def critique(topic: str, findings: list[Finding], cfg: DeepenConfig) -> CriticVerdict:
    """One structured call: judge coverage + propose next facets."""
    user = f"TOPIC:\n{topic}\n\nFINDINGS SO FAR:\n{_findings_digest(findings)}"
    return await structured_completion(
        model=cfg.critic_model,
        response_format=CriticVerdict,
        system=_CRITIC_PROMPT,
        user=user,
        temperature=cfg.temperature,
        reasoning_effort=cfg.reasoning_effort,
    )


# -- Orchestrator: the gap-following loop -------------------------------------


ProgressCb = Callable[[str], Awaitable[None]]
RoundCb = Callable[["RoundResult"], Awaitable[None]]
CoverageProbe = Callable[[list[str]], Awaitable[bool]]


async def run_deepen(
    topic: str,
    *,
    cfg: DeepenConfig,
    exploration_cfg: ExplorationConfig,
    project_id: str,
    kb_id: str,
    deepen_run_id: str = "deepen",
    on_round: RoundCb | None = None,
    coverage_probe: CoverageProbe | None = None,
    on_progress: ProgressCb | None = None,
) -> DeepenResult:
    """Run the gap-following loop. Pure of Supabase: persistence and the
    coverage check are injected (`on_round`, `coverage_probe`)."""

    async def progress(phase: str) -> None:
        if on_progress:
            await on_progress(phase)

    merger = FindingMerger(
        fuzzy_threshold=exploration_cfg.fuzzy_match_threshold,
        min_confidence=exploration_cfg.min_confidence_threshold,
    )
    seen: list[Finding] = []
    result = DeepenResult()

    facets = await decompose_topic(topic, cfg)
    sem = asyncio.Semaphore(cfg.max_concurrent_facets)

    async def _facet(facet: str) -> list[Finding]:
        async with sem:
            return await run_exploration(
                facet,
                exploration_id=deepen_run_id,
                project_id=project_id,
                kb_id=kb_id,
                cfg=exploration_cfg,
            )

    for r in range(cfg.depth_cap):
        await progress(f"round-{r}-searching")
        raw_lists = await asyncio.gather(*(_facet(f) for f in facets), return_exceptions=True)
        raw: list[Finding] = []
        for item in raw_lists:
            if isinstance(item, BaseException):
                logger.warning("deepen facet failed: %s", item)
                continue
            raw.extend(item)

        # cross-facet dedupe (within round), then cross-round filter
        round_merged = await asyncio.to_thread(merger.merge_findings, raw)
        fresh = [
            f
            for f in round_merged
            if not _is_duplicate(f, seen, exploration_cfg.fuzzy_match_threshold)
        ]
        seen.extend(fresh)

        verdict = await critique(topic, seen, cfg)
        round_result = RoundResult(round=r, facets=facets, findings=fresh, verdict=verdict)
        result.rounds.append(round_result)
        if on_round:
            await on_round(round_result)

        result.gap_set = (
            verdict.unanswered
        )  # final-round open questions (overwritten, not cumulative)
        next_facets = verdict.next_facets[: cfg.facets_per_round]

        if r + 1 >= cfg.min_rounds:
            covered = (
                bool(coverage_probe) and bool(next_facets) and await coverage_probe(next_facets)
            )
            if (
                verdict.done
                or covered
                or verdict.coverage >= cfg.coverage_target
                or not next_facets
            ):
                break
        facets = next_facets or facets

    await progress("completed")
    return result
