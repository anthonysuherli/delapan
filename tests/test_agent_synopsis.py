from __future__ import annotations

from delapan.core.agent import synopsis as syn


def test_synopsis_roundtrip_via_store(store):
    org_id, project_id = store.resolve_project("ag", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    store.upsert_synopsis(kb_id, content=[{"x": 1}], finding_count=2, model="m")
    loaded = store.load_synopsis(kb_id)
    assert loaded["content"] == [{"x": 1}]
    assert hasattr(syn, "__file__")
