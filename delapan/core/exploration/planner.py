"""Search-query planner.

Ported from delapan's `features/exploration/planner.py`. One structured-output
call via AI Gateway produces an `ExplorationPlan`. The `lens` arg selects the
framing: "explore" (entities/facts, default) or "learn" (concepts/explanations);
no ontology yet.
"""

from __future__ import annotations

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import ExplorationConfig
from delapan.core.exploration.models import ExplorationPlan

_PLANNER_PROMPT = """\
You are a search query planner for EXPLORATION mode. The user wants to discover \
entities, relationships, and factual data about a subject — who, what, where, when.

Generate 3-6 search queries with priority levels:
- Priority 1: Core queries that directly address the prompt (always run)
- Priority 2: Supplementary queries for additional context (run if few results from P1)

For each query, optionally include a domain_filter (e.g., "site:crunchbase.com") \
if a specific site is particularly relevant.

Also provide:
- extraction_prompt: DETAILED INSTRUCTIONS (not templates or examples) for extracting \
ACTUAL entities, relationships, facts, and structured data. The extraction_prompt must \
explicitly state: "Extract ACTUAL content from the pages, not template placeholders. \
Every finding must have a concrete title with real information and a content dict with \
real extracted data." Be extremely specific about required fields.
- expected_categories: labels for finding types (e.g., "funding_round", "person_role", \
"company_info", "partnership")
- finding_title_hint: a template showing format only (e.g., "{entity_name}: {entity_type}"), \
NOT to be included in extraction_prompt

Focus on discovering concrete facts, entities, and their relationships. \
Prefer exact phrases in quotes for names.

CRITICAL: The extraction_prompt must instruct the extractor to return findings with \
"title" containing ACTUAL extracted information (not placeholder text like [entity] or \
[value]), and "content" as a non-empty dict with concrete extracted facts. Also include a \
"layout_hint" field on every extracted item. Valid values: "headline", "stat", "table_row", \
"narrative", "source_list". The extractor must NEVER return template strings or empty content."""


_LEARN_PROMPT = """\
You are a search query planner for LEARN mode. The user wants to UNDERSTAND a \
topic — concepts, definitions, mechanisms, and how things relate — not a roster \
of entities and facts.

Generate 3-6 search queries with priority levels:
- Priority 1: Core explanatory queries (definitions, "how X works", fundamentals)
- Priority 2: Supplementary depth (nuances, comparisons, common misconceptions)

Also provide:
- extraction_prompt: DETAILED INSTRUCTIONS for extracting CONCEPTS and \
EXPLANATIONS. Each finding's title is the concept name; content is a dict with \
a real definition/explanation and, where relevant, prerequisites or examples. \
State explicitly: "Extract ACTUAL explanations, not template placeholders. Every \
finding must have a concrete concept title and a non-empty content dict." Include \
a "layout_hint" field; valid values: "headline", "stat", "table_row", "narrative", \
"source_list".
- expected_categories: concept-type labels (e.g. "definition", "mechanism", \
"principle", "comparison")
- finding_title_hint: format only (e.g. "{concept_name}")"""


async def plan_queries(
    prompt: str, cfg: ExplorationConfig, *, lens: str = "explore"
) -> ExplorationPlan:
    """Plan search queries + extraction instructions. `lens` selects the framing:
    "explore" (entities/facts, default) or "learn" (concepts/explanations)."""
    system = _LEARN_PROMPT if lens == "learn" else _PLANNER_PROMPT
    return await structured_completion(
        model=cfg.planner_model,
        response_format=ExplorationPlan,
        system=system,
        user=prompt,
        temperature=cfg.temperature,
        reasoning_effort=cfg.reasoning_effort,
    )
