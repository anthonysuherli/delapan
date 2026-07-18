from __future__ import annotations

import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


def _seed(store):
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb("org1", pid, "main", create=True)
    return pid, kid


def test_archive_kb_sets_timestamp(monkeypatch):
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert out["kb_id"] == kid
    assert out["archived_at"] is not None
    assert out["finding_count"] == 0


def test_unarchive_clears_timestamp(monkeypatch):
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    out = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    assert out["archived_at"] is None


def test_archive_is_idempotent(monkeypatch):
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    a = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    b = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert a["archived_at"] == b["archived_at"]


def test_archive_unknown_raises(monkeypatch):
    store, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        store.set_archived(project_id="nope", archived=True)


def test_archive_mismatched_pair_raises(monkeypatch):
    """Same contract as the SQLite tier — the pair must belong together."""
    store, _ = make_store(monkeypatch)
    pid, kid = _seed(store)
    _, other_pid = store.resolve_project("repoB", create=True)
    with pytest.raises(RuntimeError):
        store.set_archived(project_id=other_pid, kb_id=kid, archived=True)
