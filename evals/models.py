"""Question-set schema + loader.

    sets/*.yaml ──► load_question_set ──► list[Question] (validated, frozen)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

QUESTION_TYPES = frozenset({"single-hop", "multi-hop", "temporal", "unanswerable"})


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    reference_answer: str
    gold_finding_ids: list[str] = field(default_factory=list)
    type: str = "single-hop"


def load_question_set(path: Path) -> tuple[str, list[Question]]:
    """Parse and validate one question-set yaml. Raises ValueError on any
    schema violation — a bad set must never produce a silently-partial run."""
    spec = yaml.safe_load(Path(path).read_text())
    questions: list[Question] = []
    seen: set[str] = set()
    for raw in spec.get("questions", []):
        q = Question(
            id=str(raw["id"]),
            question=str(raw["question"]),
            reference_answer=str(raw.get("reference_answer", "")),
            gold_finding_ids=[str(x) for x in raw.get("gold_finding_ids", [])],
            type=str(raw.get("type", "single-hop")),
        )
        if q.id in seen:
            raise ValueError(f"duplicate question id: {q.id}")
        seen.add(q.id)
        if q.type not in QUESTION_TYPES:
            raise ValueError(f"unknown question type {q.type!r} on {q.id}")
        if q.type != "unanswerable" and not q.gold_finding_ids:
            raise ValueError(f"{q.id}: answerable questions need gold_finding_ids")
        questions.append(q)
    if not questions:
        raise ValueError(f"{path}: no questions")
    return str(spec["name"]), questions
