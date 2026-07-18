"""Both Store backends must return byte-identical shapes for the lifecycle API.

The vision invariant is that the engine cannot tell the two tiers apart. These
tests assert that directly: same calls, same keys, same values.
"""
from __future__ import annotations

import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase
from tests.test_archive_supabase import _register_rpc


@pytest.fixture()
def sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "parity.db"))
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("local", pid, "main", create=True)
    return s, pid, kid


@pytest.fixture()
def supabase_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    s = SupabaseStore("jwt", org_id="org1")
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("org1", pid, "main", create=True)
    _register_rpc(fake)
    return s, pid, kid


def _shape(store, pid, kid):
    """Run the lifecycle round-trip and return key sets + observable values."""
    archived = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    hidden = store.list_projects()
    shown = store.list_projects(include_archived=True)
    restored = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    after = store.list_projects()

    # Project-level round-trip. Supabase delegates all archived-filtering to the
    # RPC's SQL while SQLite filters in Python, so this is the case most likely
    # to diverge -- and neither tier covers it in its own suite.
    store.set_archived(project_id=pid, archived=True)
    p_hidden = store.list_projects()
    p_shown = store.list_projects(include_archived=True)
    p_restored = store.set_archived(project_id=pid, archived=False)
    p_after = store.list_projects()

    return {
        "archive_keys": sorted(archived),
        "archived_stamped": archived["archived_at"] is not None,
        "restore_stamped": restored["archived_at"] is None,
        "hidden_kbs": [k["kb"] for p in hidden for k in p["kbs"]],
        "shown_kbs": [k["kb"] for p in shown for k in p["kbs"]],
        "after_kbs": [k["kb"] for p in after for k in p["kbs"]],
        "project_keys": sorted(after[0]),
        "kb_keys": sorted(after[0]["kbs"][0]),
        # Project-level observations.
        "proj_archive_keys": sorted(p_restored),
        "proj_hidden": [p["project"] for p in p_hidden],
        "proj_shown": [p["project"] for p in p_shown],
        "proj_after": [p["project"] for p in p_after],
        # Cascade by read: the project's flag hides its KBs without stamping
        # them, which is what makes unarchive lossless.
        "kb_stamped_by_project_archive": [
            k["archived_at"] for p in p_shown for k in p["kbs"]
        ],
    }


def test_lifecycle_shapes_match_across_backends(sqlite_store, supabase_store):
    assert _shape(*sqlite_store) == _shape(*supabase_store)


def test_lifecycle_contract_is_correct(sqlite_store):
    """Pin the actual expected values, not just cross-backend agreement."""
    got = _shape(*sqlite_store)
    assert got["archive_keys"] == [
        "archived_at", "finding_count", "kb_id", "project_id"
    ]
    assert got["kb_keys"] == [
        "archived_at", "finding_count", "kb", "kb_id", "last_finding_at"
    ]
    assert got["project_keys"] == ["archived_at", "kbs", "project", "project_id"]
    assert got["archived_stamped"] is True
    assert got["restore_stamped"] is True
    assert got["hidden_kbs"] == []
    assert got["shown_kbs"] == ["main"]
    assert got["after_kbs"] == ["main"]
    # An archived project drops out entirely, and comes back on unarchive.
    assert got["proj_hidden"] == []
    assert got["proj_shown"] == ["repoA"]
    assert got["proj_after"] == ["repoA"]
    assert got["proj_archive_keys"] == [
        "archived_at", "finding_count", "kb_id", "project_id"
    ]
    # ...and its KB rows were never stamped, so unarchive is lossless.
    assert got["kb_stamped_by_project_archive"] == [None]
