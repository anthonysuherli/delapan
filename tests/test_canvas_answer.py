"""Answer synthesis: grounded system prompt + clamped history + streamed deltas."""

from __future__ import annotations

from datetime import UTC, datetime

from delapan.core.canvas import answer as answer_mod
from delapan.core.config import CanvasConfig
from delapan.core.exploration.models import Finding


def _candidate(title: str) -> Finding:
    now = datetime.now(UTC)
    return Finding(
        exploration_id="e1",
        project_id="p1",
        category="cat",
        title=title,
        content={"k": "v"},
        created_at=now,
        updated_at=now,
    )


def test_build_answer_messages_clamps_history():
    cfg = CanvasConfig(max_history_turns=2)
    history = [
        {"role": "user", "content": f"m{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"m{i}"}
        for i in range(6)
    ]
    messages = answer_mod.build_answer_messages("the question", history, cfg)
    # last 2 history messages + the new user prompt
    assert [m["content"] for m in messages] == ["m4", "m5", "the question"]
    assert messages[-1] == {"role": "user", "content": "the question"}


async def test_stream_answer_grounds_system_and_yields_deltas(monkeypatch):
    seen = {}

    async def _fake_stream(*, model, system, messages, temperature=0.2, max_tokens=None):
        seen["model"] = model
        seen["system"] = system
        seen["messages"] = messages
        for chunk in ["Hello ", "world"]:
            yield chunk

    monkeypatch.setattr(answer_mod, "stream_text_completion", _fake_stream)
    cfg = CanvasConfig()
    deltas = [
        d
        async for d in answer_mod.stream_answer(
            "q?",
            preamble_xml="<preamble><empty/></preamble>",
            candidates=[_candidate("Quota limits"), _candidate("Pricing tiers")],
            history=[],
            cfg=cfg,
        )
    ]
    assert deltas == ["Hello ", "world"]
    assert seen["model"] == cfg.answer_model
    assert "<preamble><empty/></preamble>" in seen["system"]
    assert "Quota limits" in seen["system"] and "Pricing tiers" in seen["system"]
    assert seen["messages"][-1] == {"role": "user", "content": "q?"}
