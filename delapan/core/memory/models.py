"""Resolution decision models — the typed contract between resolver and persist.

    resolver → [ResolutionDecision…]   persist applies them → ResolutionOutcome

``ResolutionBatch`` doubles as the LLM structured-output schema (one decision per
candidate, keyed by ``candidate_index``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ResolutionOp(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"
    DELETE = "DELETE"


class ResolutionDecision(BaseModel):
    """One decision for one candidate, identified by its batch position."""

    candidate_index: int = Field(description="0-based index into the candidate batch")
    op: ResolutionOp
    target_finding_id: str | None = Field(
        default=None,
        description="Existing finding id to UPDATE/NOOP/DELETE; null for ADD",
    )
    reason: str = Field(default="", description="Short justification for the op")


class ResolutionBatch(BaseModel):
    """Structured-output container — one decision per candidate needing one."""

    decisions: list[ResolutionDecision] = Field(default_factory=list)


class ResolutionEvent(BaseModel):
    """A log row describing one applied decision (persisted to resolution_events)."""

    op: str
    candidate_title: str
    target_finding_id: str | None = None
    new_finding_id: str | None = None  # row created by ADD/UPDATE/SUPERSEDE
    details: dict | None = None  # op-specific effect (NOOP: merged urls, confidence delta)
    reason: str = ""


class ResolutionOutcome(BaseModel):
    """Result of resolve_and_persist: finding ids touched + the events logged."""

    affected_finding_ids: list[str] = Field(default_factory=list)
    events: list[ResolutionEvent] = Field(default_factory=list)
