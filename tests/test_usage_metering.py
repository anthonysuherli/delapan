"""Cost metering — the leaf-client seam that feeds the operator cost console.

Metering lives in the clients (not the API routes) so every LLM/search call is
recorded regardless of entry path. These cover the recorder's costing and
attribution, and that each ai_gateway path actually calls it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from delapan.core.clients import ai_gateway
from delapan.core.monitoring.metering_context import current_metering, metering_scope
from delapan.core.monitoring.usage_recorder import record_usage


class _FakeTable:
    def __init__(self, store: dict[str, list[dict]], name: str) -> None:
        self.store, self.name, self._payload = store, name, None

    def insert(self, payload: Any) -> _FakeTable:
        self._payload = payload
        return self

    def execute(self) -> SimpleNamespace:
        items = self._payload if isinstance(self._payload, list) else [self._payload]
        self.store.setdefault(self.name, []).extend(items)
        return SimpleNamespace(data=items)


class _FakeClient:
    def __init__(self) -> None:
        self.store: dict[str, list[dict]] = {}

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self.store, name)


# --- recorder ---------------------------------------------------------------


async def test_records_costed_attributed_row():
    sb = _FakeClient()
    org, user = str(uuid.uuid4()), str(uuid.uuid4())
    with metering_scope(org_id=org, user_id=user, operation="explore"):
        await record_usage(
            model="anthropic/claude-sonnet-4.6", in_tokens=1_000_000, out_tokens=0, sb=sb
        )
    (row,) = sb.store["usage_events"]
    assert row["org_id"] == org
    assert row["user_id"] == user
    assert row["operation"] == "explore"
    assert row["model"] == "anthropic/claude-sonnet-4.6"
    assert row["in_tokens"] == 1_000_000
    assert row["cost_usd"] == pytest.approx(3.0)  # $3/1M input


async def test_no_scope_records_nulls_and_other():
    sb = _FakeClient()
    await record_usage(model="anthropic/claude-sonnet-4.6", in_tokens=0, out_tokens=0, sb=sb)
    (row,) = sb.store["usage_events"]
    assert row["org_id"] is None
    assert row["user_id"] is None
    assert row["operation"] == "other"


async def test_local_tier_ids_are_dropped_not_rejected():
    """org_id/user_id are uuid columns; the local tier's synthetic "local" must
    become NULL or the whole row would be rejected and the event lost."""
    sb = _FakeClient()
    with metering_scope(org_id="local", user_id="local", operation="explore"):
        await record_usage(model="tavily", units=1, sb=sb)
    (row,) = sb.store["usage_events"]
    assert row["org_id"] is None
    assert row["user_id"] is None
    assert row["cost_usd"] == pytest.approx(0.008)


async def test_unknown_model_costs_zero_but_still_records():
    sb = _FakeClient()
    await record_usage(model="who/knows", in_tokens=500, out_tokens=500, sb=sb)
    (row,) = sb.store["usage_events"]
    assert row["model"] == "who/knows"
    assert row["cost_usd"] == 0.0


async def test_never_raises_into_the_request_path():
    class _Boom:
        def table(self, _name: str) -> Any:
            raise RuntimeError("supabase down")

    await record_usage(model="m", in_tokens=1, out_tokens=1, sb=_Boom())  # must not raise


async def test_scope_resets_on_exit():
    with metering_scope(org_id="o", user_id="u", operation="explore"):
        assert current_metering() is not None
    assert current_metering() is None


# --- ai_gateway seam --------------------------------------------------------


@pytest.fixture()
def metered(monkeypatch):
    """Capture record_usage calls made by ai_gateway."""
    calls: list[dict] = []

    async def _rec(**kw):
        calls.append(kw)

    monkeypatch.setattr(ai_gateway, "record_usage", _rec)
    return calls


def _fake_gateway(monkeypatch, create):
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_gateway, "gateway_client", lambda: fake)


async def test_text_completion_meters(monkeypatch, metered):
    async def _create(**_kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )

    _fake_gateway(monkeypatch, _create)
    out = await ai_gateway.text_completion(
        model="anthropic/claude-sonnet-4.6", system="s", user="u"
    )
    assert out == "hi"
    (call,) = metered
    assert call == {"model": "anthropic/claude-sonnet-4.6", "in_tokens": 100, "out_tokens": 20}


class _Schema(BaseModel):
    ok: bool


async def test_structured_completion_meters(monkeypatch, metered):
    async def _create(**_kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        )

    _fake_gateway(monkeypatch, _create)
    out = await ai_gateway.structured_completion(
        model="google/gemini-3.1-pro-preview", response_format=_Schema, system="s", user="u"
    )
    assert out.ok is True
    (call,) = metered
    assert call == {"model": "google/gemini-3.1-pro-preview", "in_tokens": 7, "out_tokens": 3}


async def test_stream_text_completion_meters_final_usage_chunk(monkeypatch, metered):
    """Streaming carries usage only in a trailing usage-only chunk, which the
    client asks for via stream_options.include_usage."""
    seen_kw: dict = {}

    async def _create(**kw):
        seen_kw.update(kw)

        async def _gen():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="he"))], usage=None
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="y"))], usage=None
            )
            yield SimpleNamespace(
                choices=[], usage=SimpleNamespace(prompt_tokens=11, completion_tokens=2)
            )

        return _gen()

    _fake_gateway(monkeypatch, _create)
    chunks = [
        c
        async for c in ai_gateway.stream_text_completion(
            model="anthropic/claude-sonnet-4.6", system="s", messages=[]
        )
    ]
    assert "".join(chunks) == "hey"
    assert seen_kw["stream_options"] == {"include_usage": True}
    (call,) = metered
    assert call == {"model": "anthropic/claude-sonnet-4.6", "in_tokens": 11, "out_tokens": 2}
