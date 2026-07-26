"""The lexical-leakage gate: questions must not echo the source span."""
from __future__ import annotations

from evals.sets.generate import has_leakage

SOURCE = (
    "Project A 2026.07 adds a context evaluation harness with three ablation arms "
    "covering closed book, production, and oracle context conditions."
)


def test_verbatim_span_leaks():
    q = "Does the context evaluation harness with three ablation arms covering closed book exist?"
    assert has_leakage(q, SOURCE)                      # ≥8-token contiguous overlap


def test_paraphrase_passes():
    q = "How many experiment conditions does Project A's new eval feature compare?"
    assert not has_leakage(q, SOURCE)


def test_case_insensitive():
    q = "PROJECT A 2026.07 ADDS A CONTEXT EVALUATION HARNESS WITH THREE things?"
    assert has_leakage(q, SOURCE)                      # 8 shared tokens, case-folded


def test_short_overlap_ok():
    q = "What did Project A ship?"
    assert not has_leakage(q, SOURCE)                  # <8-token overlap is fine
