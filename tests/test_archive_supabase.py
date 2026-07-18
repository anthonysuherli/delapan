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


def _register_rpc(fake):
    """Stand in for list_projects_with_activity over the fake's tables."""
    def _fn(params):
        org, inc = params["p_org_id"], params["p_include_archived"]
        out = []
        for p in fake.tables.get("projects", []):
            if p["org_id"] != org or p["name"] == "__journal__":
                continue
            if not inc and p.get("archived_at") is not None:
                continue
            # Mirror the SQL: the archived-KB filter is part of the join, so a
            # project whose KBs are all archived still yields one filler row
            # rather than disappearing.
            kbs = [k for k in fake.tables.get("kbs", [])
                   if k["project_id"] == p["id"] and k["org_id"] == org
                   and (inc or k.get("archived_at") is None)]
            if not kbs:
                out.append({"project_id": p["id"], "project_name": p["name"],
                            "project_archived_at": p.get("archived_at"),
                            "kb_id": None, "kb_name": None,
                            "kb_archived_at": None,
                            "finding_count": 0, "last_finding_at": None})
                continue
            for k in kbs:
                live = [f for f in fake.tables.get("findings", [])
                        if f["kb_id"] == k["id"] and f.get("invalidated_at") is None]
                out.append({
                    "project_id": p["id"], "project_name": p["name"],
                    "project_archived_at": p.get("archived_at"),
                    "kb_id": k["id"], "kb_name": k["name"],
                    "kb_archived_at": k.get("archived_at"),
                    "finding_count": len(live),
                    "last_finding_at": max((f["created_at"] for f in live), default=None),
                })
        return out
    fake.register_rpc("list_projects_with_activity", _fn)


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


def test_archive_returns_db_echoed_timestamp(monkeypatch):
    """The stamp returned on write must be the one the DB echoes back, so the
    idempotent path (which rereads it) cannot disagree with the write path."""
    store, fake = make_store(monkeypatch)
    pid, kid = _seed(store)
    first = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    stored = next(k for k in fake.tables["kbs"] if k["id"] == kid)["archived_at"]
    assert first["archived_at"] == stored
    second = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    assert second["archived_at"] == stored
