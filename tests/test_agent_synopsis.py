from __future__ import annotations

import pytest

from delapan.core.agent import synopsis as syn
from delapan.core.agent import synopsis as synopsis_mod
from delapan.core.config import SynopsisConfig, get_settings


def test_synopsis_roundtrip_via_store(store):
    org_id, project_id = store.resolve_project("ag", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    store.upsert_synopsis(kb_id, content=[{"x": 1}], finding_count=2, model="m")
    loaded = store.load_synopsis(kb_id)
    assert loaded["content"] == [{"x": 1}]
    assert hasattr(syn, "__file__")


async def test_build_routes_gateway_slug_through_text_completion(monkeypatch):
    seen = {}

    async def _fake_text_completion(*, model, system, user, temperature=0.0, max_tokens=None):
        seen["model"] = model
        seen["user"] = user
        return '[{"topic": "t", "gloss": "g"}]'

    monkeypatch.setattr(synopsis_mod, "text_completion", _fake_text_completion)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "fake")
    get_settings.cache_clear()

    out = await synopsis_mod._build(
        [{"title": "A", "category": "c"}], SynopsisConfig(model="anthropic/claude-haiku-4.5")
    )
    assert seen["model"] == "anthropic/claude-haiku-4.5"
    # Assert the rendered prompt reached text_completion with the correct findings data.
    assert "A" in seen["user"]
    assert "c" in seen["user"]
    assert "JSON" in seen["user"]
    assert out == [{"topic": "t", "gloss": "g"}]
    get_settings.cache_clear()


async def test_build_gateway_slug_without_key_raises_actionable(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc_info:
        await synopsis_mod._build([{"title": "A"}], SynopsisConfig(model="anthropic/claude-haiku-4.5"))
    assert "AI_GATEWAY_API_KEY" in str(exc_info.value)
    get_settings.cache_clear()


async def test_direct_slug_without_anthropic_key_raises_actionable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc_info:
        await synopsis_mod._build([{"title": "A"}], SynopsisConfig(model="claude-haiku-4-5"))
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)
    get_settings.cache_clear()


async def test_maybe_rebuild_returns_status_strings(store, monkeypatch):
    org, pid = store.resolve_project("synp", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)

    # Empty KB → skipped (should_rebuild is False at live_count == 0).
    assert await synopsis_mod.maybe_rebuild_synopsis(kb, store=store) == "skipped"

    await store.insert_findings(
        [
            {
                "org_id": org,
                "kb_id": kb,
                "title": "T",
                "content": "c",
                "category": "cat",
                "confidence": 0.5,
                "tags": [],
                "provenance": [],
                "embedding": [0.01] * 1536,
            }
        ]
    )

    async def _boom(findings, cfg):
        raise RuntimeError("no key")

    monkeypatch.setattr(synopsis_mod, "_build", _boom)
    status = await synopsis_mod.maybe_rebuild_synopsis(kb, store=store)
    assert status.startswith("failed: ")
    assert "no key" in status

    async def _ok(findings, cfg):
        return [{"topic": "t", "gloss": "g"}]

    monkeypatch.setattr(synopsis_mod, "_build", _ok)
    assert await synopsis_mod.maybe_rebuild_synopsis(kb, store=store) == "rebuilt"
