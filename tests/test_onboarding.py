"""Onboarding card: KB-not-found must guide, not just error."""

from __future__ import annotations

import pytest


@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "onb.db"))
    from delapan.store import get_store

    return get_store()


def test_card_has_guidance_without_demo(local_store):
    from delapan.mcp.onboarding import kb_not_found_card

    card = kb_not_found_card("myrepo", "main", ValueError("nope"), store=local_store)
    assert card["error"].startswith("KB not found (myrepo/main)")
    assert card["coverage"] == "gap"
    assert "/delapan:explore" in card["onboarding"]
    assert "try_demo" not in card  # empty store → no demo project → self-suppressed


def test_card_offers_demo_when_present(local_store):
    from delapan.mcp.onboarding import DEMO_KB, DEMO_PROJECT, kb_not_found_card

    org_id, project_id = local_store.resolve_project(DEMO_PROJECT, create=True)
    local_store.resolve_kb(org_id, project_id, DEMO_KB, create=True)
    card = kb_not_found_card("myrepo", "main", ValueError("nope"), store=local_store)
    assert DEMO_PROJECT in card["try_demo"] and DEMO_KB in card["try_demo"]


def test_card_survives_store_none():
    from delapan.mcp.onboarding import kb_not_found_card

    card = kb_not_found_card("a", "b", RuntimeError("x"), store=None)
    assert "onboarding" in card and "try_demo" not in card


@pytest.mark.asyncio
async def test_resume_tool_returns_card(local_store, monkeypatch):
    import delapan.mcp.server as s

    res = await s.delapan_resume("ghost-project", "ghost-kb")
    assert "onboarding" in res and res["coverage"] == "gap"


@pytest.mark.asyncio
async def test_resume_card_survives_store_resolution_failure(local_store, monkeypatch):
    import delapan.mcp.server as s

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(s, "resolve_store", _boom)
    res = await s.delapan_resume("ghost-project", "ghost-kb")
    assert "onboarding" in res and "try_demo" not in res
