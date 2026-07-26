"""Reference-based correctness judge — rubric + CoT, structured verdict.

    question + reference + answer ──► structured_completion ──► CorrectnessVerdict
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from delapan.core.clients.ai_gateway import structured_completion
from evals.models import Question

JUDGE_SYSTEM = (
    "You grade an answer against a reference answer. Reason step by step in "
    "`reasoning`, then give `verdict`:\n"
    "- correct: the answer conveys the reference's substance (wording may differ; "
    "extra correct detail is fine).\n"
    "- incorrect: it contradicts the reference, is wrong, or answers something else.\n"
    "- abstained: it declines to answer or says the information is unavailable.\n"
    "Judge substance only — never length, style, or confidence."
)

_USER_TEMPLATE = (
    "Question: {question}\n\nReference answer: {reference}\n\nAnswer to grade: {answer}"
)


class CorrectnessVerdict(BaseModel):
    reasoning: str = Field(description="Step-by-step comparison against the reference")
    verdict: Literal["correct", "incorrect", "abstained"]


async def judge(question: Question, answer: str, *, judge_model: str) -> CorrectnessVerdict:
    return await structured_completion(
        model=judge_model,
        response_format=CorrectnessVerdict,
        system=JUDGE_SYSTEM,
        user=_USER_TEMPLATE.format(
            question=question.question,
            reference=question.reference_answer or "(none — this question is unanswerable)",
            answer=answer,
        ),
    )


def is_correct(question_type: str, verdict: str) -> bool:
    """Unanswerable questions: abstention IS the correct behavior (and only it)."""
    if question_type == "unanswerable":
        return verdict == "abstained"
    return verdict == "correct"
