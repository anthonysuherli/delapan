"""Question-set loading: schema validation, duplicate ids, unanswerable rules."""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.models import load_question_set

VALID = """\
name: sample
questions:
  - id: q001
    question: What model does delapan use for embeddings?
    reference_answer: google/gemini-embedding-001 at 1536 dimensions.
    gold_finding_ids: [f1, f2]
    type: single-hop
  - id: q002
    question: What is the airspeed velocity of an unladen swallow?
    reference_answer: ""
    gold_finding_ids: []
    type: unanswerable
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "set.yaml"
    p.write_text(text)
    return p


def test_load_valid_set(tmp_path):
    name, qs = load_question_set(_write(tmp_path, VALID))
    assert name == "sample"
    assert [q.id for q in qs] == ["q001", "q002"]
    assert qs[0].gold_finding_ids == ["f1", "f2"]
    assert qs[1].type == "unanswerable"


def test_duplicate_ids_rejected(tmp_path):
    bad = VALID.replace("q002", "q001")
    with pytest.raises(ValueError, match="duplicate"):
        load_question_set(_write(tmp_path, bad))


def test_unknown_type_rejected(tmp_path):
    bad = VALID.replace("single-hop", "essay")
    with pytest.raises(ValueError, match="type"):
        load_question_set(_write(tmp_path, bad))


def test_answerable_requires_gold_ids(tmp_path):
    bad = VALID.replace("gold_finding_ids: [f1, f2]", "gold_finding_ids: []")
    with pytest.raises(ValueError, match="gold_finding_ids"):
        load_question_set(_write(tmp_path, bad))
