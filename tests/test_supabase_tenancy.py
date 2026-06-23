import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(
        "delapan.store.supabase.user_client", lambda _t: fake
    )
    store = SupabaseStore("jwt", org_id="org1")
    return store, fake


def test_vec_encoding():
    assert SupabaseStore._vec([0.5, -1.0]) == "[0.5,-1.0]"


def test_resolve_project_find_or_create(monkeypatch):
    store, fake = make_store(monkeypatch)
    org, pid = store.resolve_project("repoA", create=True)
    assert org == "org1" and pid
    # second resolve finds the same row
    org2, pid2 = store.resolve_project("repoA", create=False)
    assert (org2, pid2) == (org, pid)
    assert fake.tables["projects"][0]["org_id"] == "org1"


def test_resolve_kb_not_found_raises(monkeypatch):
    store, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        store.resolve_kb("org1", "p1", "missing", create=False)


def test_list_projects_shape(monkeypatch):
    store, fake = make_store(monkeypatch)
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb("org1", pid, "main", create=True)
    out = store.list_projects()
    assert out == [{"project": "repoA", "project_id": pid,
                    "kbs": [{"kb": "main", "kb_id": kid,
                             "snapshot_count": 0, "last_activity": None}]}]
