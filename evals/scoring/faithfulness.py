"""Faithfulness via a local trained detector (HHEM-2.1-Open), guarded.

    context xml + answer ──► guard: findings present? ──► HHEM(premise, hypothesis)
                                        └─ no ──► None (exempt — never score empty context)
"""

from __future__ import annotations

from collections.abc import Callable

Predictor = Callable[[str, str], float]

_HHEM_ID = "vectara/hallucination_evaluation_model"


def score_faithfulness(
    context_xml: str | None, answer: str, predictor: Predictor | None = None
) -> float | None:
    """Consistency score in [0,1] of `answer` against injected findings, or None.

    None means EXEMPT, not perfect: reference-free faithfulness degenerates to
    ~1.0 on empty context, so records with no injected findings must never be
    averaged into the faithfulness rate."""
    if not context_xml or "<finding " not in context_xml:
        return None
    predictor = predictor or load_hhem()
    return float(predictor(context_xml, answer))


def load_hhem() -> Predictor:
    """Lazy-load HHEM-2.1-Open (CPU, <600MB). Import cost paid once per process."""
    try:
        from transformers import AutoModelForSequenceClassification
    except ImportError as exc:  # pragma: no cover - message content tested via fake import
        raise ImportError(
            'HHEM needs the optional extra: uv pip install -e ".[evals-hhem]"'
        ) from exc

    model = AutoModelForSequenceClassification.from_pretrained(_HHEM_ID, trust_remote_code=True)

    def predict(premise: str, hypothesis: str) -> float:
        return float(model.predict([(premise, hypothesis)]).item())

    return predict
