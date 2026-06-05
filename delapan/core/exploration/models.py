"""Pydantic models for the exploration pipeline.

Ported from delapan's `features/exploration/models.py`, trimmed to what
delapan uses: no `TolerantModel`, no `Exploration` row (delapan owns that
row in Supabase), no ontology. `RawFinding`/`FindingBatch` are the LLM
structured-output schemas; `Finding` is the in-memory result the engine returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# Planner output
# -----------------------------------------------------------------------------


class SearchQuery(BaseModel):
    query: str
    domain_filter: str = ""
    priority: int = 1
    max_results: int = 20


class ExplorationPlan(BaseModel):
    search_queries: list[SearchQuery]
    extraction_prompt: str
    expected_categories: list[str]
    finding_title_hint: str


# -----------------------------------------------------------------------------
# Extractor output (LLM structured-output schemas)
# -----------------------------------------------------------------------------


class RawFinding(BaseModel):
    """One extracted finding. A structured schema (vs. bare dict) so strict
    JSON-schema providers populate fields instead of returning empty objects."""

    title: str = Field(description="Concise, specific title for this finding")
    content: dict[str, Any] = Field(
        description="Key facts extracted from the source as key-value pairs"
    )
    category: str = Field(description="Topic category for this finding")
    confidence: float = Field(default=0.7, description="Confidence score 0-1")
    entity_type: str | None = Field(default=None)
    relationships: list[dict[str, Any]] | None = Field(default=None)
    layout_hint: str | None = Field(default=None)


class FindingBatch(BaseModel):
    """Container so the extractor's structured output has a top-level object
    (OpenAI-style structured outputs reject a bare top-level array)."""

    findings: list[RawFinding] = Field(default_factory=list)


@dataclass
class ExtractionResult:
    findings: list[dict]
    source_url: str = ""
    source_query: str = ""
    model_used: str = ""


# -----------------------------------------------------------------------------
# Pipeline result
# -----------------------------------------------------------------------------


class Source(BaseModel):
    url: str
    title: str | None = None
    domain: str
    search_query: str
    was_crawled: bool = False
    crawled_at: datetime | None = None


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    exploration_id: str
    project_id: str
    kb: str = "default"
    category: str
    title: str
    content: dict
    confidence: float = 0.0
    # Reflection-critic signal in [0,1]: how specific/factual this finding is.
    # 1.0 = unevaluated (no penalty); multiplies source confidence in the merger.
    quality: float = 1.0
    source_count: int = 1
    provenance: list[dict] = Field(default_factory=list)
    extraction_model: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    tags: list[str] = Field(default_factory=list)
    entity_type: str | None = None
    relationships: list[dict] | None = None
    layout_hint: str | None = None
