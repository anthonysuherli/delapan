"""Judge wiring (mocked LLM) + the pure unanswerable scoring rule."""
from __future__ import annotations

from evals.models import Question
from evals.scoring.correctness import CorrectnessVerdict, is_correct, judge

Q = Question(id="q1", question="capital of France?", reference_answer="Paris",
             gold_finding_ids=["f1"])


async def test_judge_passes_reference_and_parses(monkeypatch):
    captured: dict = {}

    async def fake_structured(*, model, response_format, system, user, **kw):
        captured.update(model=model, user=user)
        return CorrectnessVerdict(verdict="correct", reasoning="matches reference")

    monkeypatch.setattr("evals.scoring.correctness.structured_completion", fake_structured)
    v = await judge(Q, "Paris.", judge_model="j1")
    assert v.verdict == "correct"
    assert captured["model"] == "j1"
    assert "Paris" in captured["user"] and "capital of France?" in captured["user"]


def test_is_correct_rules():
    assert is_correct("single-hop", "correct")
    assert not is_correct("single-hop", "abstained")   # abstaining on answerable ≠ correct
    assert is_correct("unanswerable", "abstained")     # abstention is the right answer
    assert not is_correct("unanswerable", "correct")   # confident answer to unanswerable
