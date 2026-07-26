"""First-run seed: copy the bundled demo KB into place — once, local tier only."""

from __future__ import annotations

import pytest


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "fresh.db"))
    return tmp_path / "fresh.db"


def test_seed_copies_demo_when_absent(fresh):
    from delapan.mcp.onboarding import seed_demo_if_absent
    from delapan.store.sqlite import SQLiteStore

    seed_demo_if_absent()
    assert fresh.exists()
    store = SQLiteStore(str(fresh))
    assert any(p["project"] == "delapan" for p in store.list_projects(include_archived=True))


def test_seed_never_touches_existing_db(fresh):
    from delapan.mcp.onboarding import seed_demo_if_absent

    fresh.write_bytes(b"user data")
    seed_demo_if_absent()
    assert fresh.read_bytes() == b"user data"


def test_seed_skips_cloud_tier(tmp_path, monkeypatch):
    from delapan.mcp.onboarding import seed_demo_if_absent

    monkeypatch.setenv("DELAPAN_BACKEND", "cloud")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "cloud.db"))
    seed_demo_if_absent()
    assert not (tmp_path / "cloud.db").exists()
