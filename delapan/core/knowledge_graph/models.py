"""LLM extraction schemas for the KG builder.

`properties` are free-form dicts, so the extractor calls `structured_completion`
with `use_json_schema=False` (strict json_schema forces unspecified object fields
to `{}` — the same gotcha the finding extractor avoids; see CLAUDE.md).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KGNodeExtract(BaseModel):
    """One entity. `label` is its canonical name; `type` its ontology class."""

    label: str = Field(description="Canonical name of the entity (e.g. 'Anduril Industries')")
    type: str = Field(description="Entity type, e.g. company | person | technology | concept")
    properties: dict[str, Any] = Field(
        default_factory=dict, description="Free-form attributes extracted for this entity"
    )
    grounded_in: list[str] = Field(
        default_factory=list,
        description="Finding ids that evidence this entity (the (finding_id: …) markers)",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternate surface forms folded into this entity during resolution",
    )


class KGEdgeExtract(BaseModel):
    """A directed relation between two entities, named by their labels."""

    source: str = Field(description="Label of the source entity")
    target: str = Field(description="Label of the target entity")
    relation: str = Field(description="Relation type, a short verb phrase (e.g. 'acquired')")
    properties: dict[str, Any] = Field(default_factory=dict)
    grounded_in: list[str] = Field(
        default_factory=list,
        description="Finding ids that evidence this relationship",
    )


class KGExtraction(BaseModel):
    """Top-level object (providers reject a bare top-level array)."""

    nodes: list[KGNodeExtract] = Field(default_factory=list)
    edges: list[KGEdgeExtract] = Field(default_factory=list)
