"""Tracking row shapes — mirror of docs/tracking markdown + Supabase tables."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InitiativeRow:
    slug: str
    title: str
    status: str
    repo: str
    blocked_by: list[str] = field(default_factory=list)
    spec: str | None = None
    plan: str | None = None
    branch: str | None = None
    updated: str = ""
    body_md: str = ""


@dataclass
class BacklogItem:
    position: int
    text: str
    repo: str = "both"
    initiative_slug: str | None = None
