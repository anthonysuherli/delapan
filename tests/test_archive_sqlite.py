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


def test_archive_mismatched_pair_raises(store):
    """A kb_id that doesn't belong to project_id must raise, not silently pass."""
    pid, kid = _seed(store)
    _, other_pid = store.resolve_project("repoB", create=True)
    with pytest.raises(RuntimeError):
        store.set_archived(project_id=other_pid, kb_id=kid, archived=True)


async def test_list_projects_reports_real_finding_activity(store):
    pid, kid = _seed(store)
    await store.insert_findings([{
        "org_id": _ORG, "kb_id": kid, "title": "t", "content": "c",
        "category": "anything-at-all", "confidence": 1.0,
        "tags": [], "provenance": {}, "embedding": [0.0] * 1536,
    }])
    [proj] = store.list_projects()
    [kb] = proj["kbs"]
    assert kb["finding_count"] == 1
    assert kb["last_finding_at"] is not None
    assert "snapshot_count" not in kb
    assert "last_activity" not in kb


async def test_superseded_findings_do_not_inflate_the_count(store):
    """Bi-temporal exclusion — retired rows must not count as activity."""
    pid, kid = _seed(store)
    [fid] = await store.insert_findings([{
        "org_id": _ORG, "kb_id": kid, "title": "t", "content": "c",
        "category": "x", "confidence": 1.0,
        "tags": [], "provenance": {}, "embedding": [0.0] * 1536,
    }])
    await store.invalidate_finding(kid, fid)
    [proj] = store.list_projects()
    assert proj["kbs"][0]["finding_count"] == 0
    assert store.set_archived(
        project_id=pid, kb_id=kid, archived=True
    )["finding_count"] == 0


def test_archived_kb_hidden_by_default(store):
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    [proj] = store.list_projects()
    assert proj["kbs"] == []
    [proj_all] = store.list_projects(include_archived=True)
    assert proj_all["kbs"][0]["archived_at"] is not None


def test_archived_project_hidden_by_default(store):
    pid, _ = _seed(store)
    store.set_archived(project_id=pid, archived=True)
    assert store.list_projects() == []
    assert len(store.list_projects(include_archived=True)) == 1


def test_project_archive_does_not_stamp_its_kbs(store):
    """Cascade-by-read: the KB row keeps its own NULL so unarchive is lossless."""
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, archived=True)
    [proj] = store.list_projects(include_archived=True)
    assert proj["archived_at"] is not None
    assert proj["kbs"][0]["archived_at"] is None
    store.set_archived(project_id=pid, archived=False)
    [restored] = store.list_projects()
    assert len(restored["kbs"]) == 1
