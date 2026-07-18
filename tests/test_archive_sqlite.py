from __future__ import annotations

import pytest

_ORG = "local"


def _seed(store):
    """A project with one KB → (project_id, kb_id)."""
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb(_ORG, pid, "main", create=True)
    return pid, kid


def test_archive_kb_sets_timestamp(store):
    pid, kid = _seed(store)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert out["project_id"] == pid
    assert out["kb_id"] == kid
    assert out["archived_at"] is not None
    assert out["finding_count"] == 0


def test_unarchive_clears_timestamp(store):
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    assert out["archived_at"] is None


def test_archive_is_idempotent(store):
    pid, kid = _seed(store)
    first = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    second = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert first["archived_at"] == second["archived_at"]


def test_archive_project_level(store):
    pid, _ = _seed(store)
    out = store.set_archived(project_id=pid, archived=True)
    assert out["kb_id"] is None
    assert out["archived_at"] is not None


def test_archive_unknown_raises(store):
    with pytest.raises(RuntimeError):
        store.set_archived(project_id="nope", archived=True)
