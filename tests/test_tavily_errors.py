"""Provider failures must raise TavilyError — never silently degrade to empty."""

from __future__ import annotations

import pytest

from delapan.core.clients import tavily as tavily_mod
from delapan.core.clients.tavily import TavilyError


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Skip the exponential-backoff sleeps."""

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(tavily_mod.asyncio, "sleep", _no_sleep)


class _BoomClient:
    """Fake AsyncTavilyClient whose calls always raise (e.g. HTTP 432 quota)."""

    async def search(self, **kwargs):
        raise RuntimeError("432 Client Error: quota exceeded")

    async def extract(self, **kwargs):
        raise RuntimeError("432 Client Error: quota exceeded")


async def test_search_raises_tavily_error_after_retries(monkeypatch):
    monkeypatch.setattr(tavily_mod, "_client", lambda: _BoomClient())
    with pytest.raises(TavilyError) as exc_info:
        await tavily_mod.search("q", max_results=5, search_depth="basic")
    assert "quota" in str(exc_info.value)


async def test_extract_total_failure_raises(monkeypatch):
    monkeypatch.setattr(tavily_mod, "_client", lambda: _BoomClient())
    with pytest.raises(TavilyError):
        await tavily_mod.extract(["http://a", "http://b"])


async def test_extract_partial_batch_failure_returns_partial(monkeypatch):
    calls = {"n": 0}

    async def _batch(urls, extract_depth):
        calls["n"] += 1
        if calls["n"] == 1:
            return {u: "content" for u in urls}
        raise TavilyError("second batch quota")

    monkeypatch.setattr(tavily_mod, "_extract_batch", _batch)
    # 25 urls → two batches of 20 + 5; batch 2 fails, batch 1's content survives.
    urls = [f"http://u{i}" for i in range(25)]
    out = await tavily_mod.extract(urls)
    assert len(out) == 20 and out["http://u0"] == "content"


async def test_extract_empty_urls_is_empty_no_error():
    assert await tavily_mod.extract([]) == {}
