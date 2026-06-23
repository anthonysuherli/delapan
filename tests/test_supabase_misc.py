import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


def test_synopsis_roundtrip(monkeypatch):
    store, _ = make_store(monkeypatch)
    store.upsert_synopsis("kb1", content=[{"h": "x"}], finding_count=3, model="m")
    syn = store.load_synopsis("kb1")
    assert syn["content"] == [{"h": "x"}] and syn["finding_count_at_build"] == 3


def test_load_synopsis_none(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.load_synopsis("kb1") is None


def test_exploration_lifecycle(monkeypatch):
    store, _ = make_store(monkeypatch)
    eid = store.create_exploration("org1", "kb1", "find things")
    store.update_exploration(eid, status="done", finding_ids=["f1"], junk="ignored")
    got = store.get_exploration(eid)
    assert got["status"] == "done" and got["finding_ids"] == ["f1"]


def test_kg_intent_versions(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.get_kg_intent("kb1") is None
    r1 = store.set_kg_intent("org1", "kb1", {"v": 1})
    r2 = store.set_kg_intent("org1", "kb1", {"v": 2})
    assert r1["version"] == 1 and r2["version"] == 2
    assert store.get_kg_intent("kb1")["version"] == 2


@pytest.mark.asyncio
async def test_record_access_never_raises(monkeypatch):
    store, _ = make_store(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(store._c, "table", boom)
    await store.record_access(org_id="org1", kb_id="kb1", surface="s", targets=[])
