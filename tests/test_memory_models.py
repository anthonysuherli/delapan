from __future__ import annotations


def test_resolution_models_basic():
    from delapan.core.memory.models import (
        ResolutionBatch,
        ResolutionDecision,
        ResolutionOp,
        ResolutionOutcome,
    )

    b = ResolutionBatch(
        decisions=[ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]
    )
    assert b.decisions[0].op == ResolutionOp.ADD
    assert b.decisions[0].target_finding_id is None

    # round-trips through JSON (it is an LLM structured-output schema)
    parsed = ResolutionBatch.model_validate_json(b.model_dump_json())
    assert parsed.decisions[0].candidate_index == 0

    out = ResolutionOutcome(affected_finding_ids=["a", "b"])
    assert out.affected_finding_ids == ["a", "b"]
    assert out.events == []
