"""Answerer builds the right system prompt per arm; abstention rule is present."""
from __future__ import annotations

from evals.answerer import ABSTAIN_MARKER, answer_question


async def test_grounded_prompt_includes_context(monkeypatch):
    calls: dict = {}

    async def fake_completion(*, model, system, user, temperature=0.0, max_tokens=None):
        calls.update(model=model, system=system, user=user)
        return "42"

    monkeypatch.setattr("evals.answerer.text_completion", fake_completion)
    out = await answer_question("q?", "<preamble>ctx</preamble>", model="m1")
    assert out == "42"
    assert calls["model"] == "m1" and calls["user"] == "q?"
    assert "<preamble>ctx</preamble>" in calls["system"]
    assert ABSTAIN_MARKER in calls["system"]


async def test_closed_book_prompt_has_no_context_block(monkeypatch):
    calls: dict = {}

    async def fake_completion(*, model, system, user, temperature=0.0, max_tokens=None):
        calls.update(system=system)
        return "x"

    monkeypatch.setattr("evals.answerer.text_completion", fake_completion)
    await answer_question("q?", None, model="m1")
    assert "preamble" not in calls["system"].lower()
    assert ABSTAIN_MARKER in calls["system"]
