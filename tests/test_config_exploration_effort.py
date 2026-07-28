from __future__ import annotations

from delapan.core.config import ExplorationConfig


def test_per_stage_effort_defaults_to_shared_value():
    """Both stage knobs inherit the shared knob when unset — no behavior change."""
    cfg = ExplorationConfig(reasoning_effort="high")
    assert cfg.planner_reasoning_effort == "high"
    assert cfg.extraction_reasoning_effort == "high"


def test_per_stage_effort_overrides_shared_value():
    """An explicit stage value wins over the shared knob."""
    cfg = ExplorationConfig(reasoning_effort="high", extraction_reasoning_effort="medium")
    assert cfg.planner_reasoning_effort == "high"
    assert cfg.extraction_reasoning_effort == "medium"


def test_shared_effort_none_leaves_stages_none():
    """None means provider default; the split must not invent a value."""
    cfg = ExplorationConfig()
    assert cfg.reasoning_effort is None
    assert cfg.planner_reasoning_effort is None
    assert cfg.extraction_reasoning_effort is None
