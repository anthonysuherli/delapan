"""Both Store backends must return byte-identical shapes for the lifecycle API.

The vision invariant is that the engine cannot tell the two tiers apart. These
tests assert that directly: same calls, same keys, same values.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase
from tests.test_archive_supabase import _register_rpc

_EMBED = [0.0] * 1536


def _finding_row(org: str, kb_id: str, title: str) -> dict:
    return {
        "org_id": org, "kb_id": kb_id, "title": title, "content": "c",
        "category": "x", "confidence": 1.0,
        "tags": [], "provenance": {}, "embedding": _EMBED,
    }


async def _seed_findings(store, org: str, kb_id: str) -> None:
    """One live finding and one retired one.

    Without findings, finding_count and last_finding_at are constant 0/None on
    both tiers and contribute nothing to the comparison. The retired one also
    pins the invalidated_at filter: a backend that counted it would report 2.
    """
    await store.insert_findings([_finding_row(org, kb_id, "live")])
    [dead] = await store.insert_findings([_finding_row(org, kb_id, "dead")])
    await store.invalidate_finding(kb_id, dead)


@pytest.fixture()
async def sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "parity.db"))
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("local", pid, "main", create=True)
    await _seed_findings(s, "local", kid)
    return s, pid, kid


@pytest.fixture()
async def supabase_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    s = SupabaseStore("jwt", org_id="org1")
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("org1", pid, "main", create=True)
    _register_rpc(fake)
    await _seed_findings(s, "org1", kid)
    return s, pid, kid


def _parses_as_timestamp(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _shape(store, pid, kid) -> dict:
    """Run both lifecycle round-trips and return every observable difference."""
    before = store.list_projects()
    kb_before = before[0]["kbs"][0]

    archived = store.set_archived(project_id=pid, kb_id=kid, archived=True)
    hidden = store.list_projects()
    shown = store.list_projects(include_archived=True)
    restored = store.set_archived(project_id=pid, kb_id=kid, archived=False)
    after = store.list_projects()

    store.set_archived(project_id=pid, archived=True)
    p_hidden = store.list_projects()
    p_shown = store.list_projects(include_archived=True)
    p_restored = store.set_archived(project_id=pid, archived=False)
    p_after = store.list_projects()

    return {
        "archive_keys": sorted(archived),
        "archived_stamped": archived["archived_at"] is not None,
        "restore_stamped": restored["archived_at"] is None,
        "archive_finding_count": archived["finding_count"],
        # Project identity, not just KB names. A backend that drops a project
        # whose KBs are all archived must not look identical to one that keeps
        # it with an empty kbs list.
        "hidden_projects": [p["project"] for p in hidden],
        "shown_projects": [p["project"] for p in shown],
        "after_projects": [p["project"] for p in after],
        "hidden_kbs": [k["kb"] for p in hidden for k in p["kbs"]],
        "shown_kbs": [k["kb"] for p in shown for k in p["kbs"]],
        "after_kbs": [k["kb"] for p in after for k in p["kbs"]],
        "project_keys": sorted(after[0]),
        "kb_keys": sorted(after[0]["kbs"][0]),
        # Activity values, not merely their keys.
        "finding_count": kb_before["finding_count"],
        "last_finding_wellformed": _parses_as_timestamp(kb_before["last_finding_at"]),
        "proj_archive_keys": sorted(p_restored),
        "proj_archive_finding_count": p_restored["finding_count"],
        "proj_hidden": [p["project"] for p in p_hidden],
        "proj_shown": [p["project"] for p in p_shown],
        "proj_after": [p["project"] for p in p_after],
        # Cascade by read: the project's flag hides its KBs without stamping
        # them, which is what makes unarchive lossless.
        "kb_stamped_by_project_archive": [
            k["archived_at"] for p in p_shown for k in p["kbs"]
        ],
    }


async def test_lifecycle_shapes_match_across_backends(sqlite_store, supabase_store):
    assert _shape(*sqlite_store) == _shape(*supabase_store)


async def test_lifecycle_contract_is_correct(sqlite_store):
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
    # One live finding; the retired one must not count.
    assert got["finding_count"] == 1
    assert got["archive_finding_count"] == 1
    assert got["last_finding_wellformed"] is True
    # Archiving the KB leaves the project present but empty.
    assert got["hidden_projects"] == ["repoA"]
    assert got["hidden_kbs"] == []
    assert got["shown_kbs"] == ["main"]
    assert got["after_kbs"] == ["main"]
    assert got["after_projects"] == ["repoA"]
    # An archived project drops out entirely, and comes back on unarchive.
    assert got["proj_hidden"] == []
    assert got["proj_shown"] == ["repoA"]
    assert got["proj_after"] == ["repoA"]
    assert got["proj_archive_keys"] == [
        "archived_at", "finding_count", "kb_id", "project_id"
    ]
    assert got["kb_stamped_by_project_archive"] == [None]
