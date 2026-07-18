from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.mcp import server


@pytest.fixture()
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "mcp.db"))
    from delapan.store import get_store

    s = get_store()
    _, pid = s.resolve_project("repoA", create=True)
    kid = s.resolve_kb("local", pid, "main", create=True)
    return s, pid, kid


async def test_archive_tool_round_trip(local):
    out = await server.delapan_archive("repoA", "main", archived=True)
    assert out["archived"] is True
    assert out["archived_at"] is not None
    assert out["finding_count"] == 0
    assert (await server.delapan_projects())["projects"][0]["kbs"] == []

    back = await server.delapan_archive("repoA", "main", archived=False)
    assert back["archived_at"] is None
    assert (await server.delapan_projects())["projects"][0]["kbs"]


async def test_archive_tool_unknown_kb_errors(local):
    out = await server.delapan_archive("repoA", "nope", archived=True)
    assert "error" in out


async def test_projects_include_archived(local):
    await server.delapan_archive("repoA", "main", archived=True)
    shown = await server.delapan_projects(include_archived=True)
    assert shown["projects"][0]["kbs"][0]["archived_at"] is not None


async def test_reads_still_work_on_archived_kb(local):
    """Archive hides from listings only — resume/search stay fully functional."""
    await server.delapan_archive("repoA", "main", archived=True)
    resumed = await server.delapan_resume("repoA", "main")
    assert "error" not in resumed
    assert "preamble" in resumed


def test_clear_archive_unarchives_kb_and_project(local):
    """The explore auto-unarchive helper, exercised without running a pipeline."""
    store, pid, kid = local
    store.set_archived(project_id=pid, kb_id=kid, archived=True)
    store.set_archived(project_id=pid, archived=True)

    ctx = SimpleNamespace(project_id=pid, kb_id=kid)
    assert server._clear_archive(store, ctx) is True

    [proj] = store.list_projects()
    assert proj["archived_at"] is None
    assert proj["kbs"][0]["archived_at"] is None
    # Second call is a no-op — nothing left to clear.
    assert server._clear_archive(store, ctx) is False
