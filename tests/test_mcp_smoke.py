from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_resume_search_projects_on_seeded_kb(monkeypatch, tmp_path):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "smoke.db"))
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()

    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("smoke", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    await store.insert_findings(
        [
            {
                "id": "seed0001",
                "org_id": org_id,
                "kb_id": kb_id,
                "title": "Seed",
                "content": {"summary": "seeded fact"},
                "category": "fact",
                "confidence": 0.9,
                "tags": [],
                "provenance": [],
                "embedding": [0.02] * 1536,
            }
        ]
    )

    import delapan.mcp.server as s

    # projects lists the seeded project
    projs = await s.delapan_projects()
    assert any(p.get("project") == "smoke" for p in projs["projects"])

    # search returns the seeded finding — stub the async embed at the SERVER namespace
    async def _fake_embed(_text):
        return [0.02] * 1536

    monkeypatch.setattr(s, "embed_text", _fake_embed, raising=True)
    res = await s.delapan_search("smoke", "main", "anything")
    assert res["findings"], "search should return the seeded finding"

    # resume with no query → no embedding/network needed
    card = await s.delapan_resume("smoke", "main")
    assert "preamble" in card and "banner" in card
