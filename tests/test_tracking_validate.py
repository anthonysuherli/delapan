from __future__ import annotations

from pathlib import Path

import pytest

from delapan.tracking.models import BacklogItem, InitiativeRow
from delapan.tracking.validate import TrackingValidationError, validate_tracking

ROOT = Path(__file__).resolve().parents[1]  # backend repo root


def _init(**kwargs) -> InitiativeRow:
    base = dict(
        slug="alpha",
        title="Alpha",
        status="active",
        repo="backend",
        blocked_by=[],
        spec=None,
        plan=None,
        branch=None,
        updated="2026-07-17",
        body_md="x",
    )
    base.update(kwargs)
    return InitiativeRow(**base)


def test_ok_with_empty_backlog() -> None:
    validate_tracking([_init()], [], repo_root=ROOT)


def test_unknown_status_fails() -> None:
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking([_init(status="shipping")], [], repo_root=ROOT)
    assert any("status" in e for e in ei.value.errors)


def test_dangling_blocked_by_fails() -> None:
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking([_init(blocked_by=["nope"])], [], repo_root=ROOT)
    assert any("blocked_by" in e for e in ei.value.errors)


def test_missing_relative_spec_fails() -> None:
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking(
            [_init(spec="docs/does-not-exist.md")],
            [],
            repo_root=ROOT,
        )
    assert any("spec" in e for e in ei.value.errors)


def test_https_spec_skips_disk_check() -> None:
    validate_tracking(
        [_init(spec="https://example.com/spec.md")],
        [],
        repo_root=ROOT,
    )


def test_duplicate_slugs_fail() -> None:
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking([_init(), _init()], [], repo_root=ROOT)
    assert any("duplicate" in e.lower() for e in ei.value.errors)
