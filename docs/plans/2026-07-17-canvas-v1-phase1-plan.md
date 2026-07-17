# Canvas v1 — Phase 1 (hardening + canvas surface) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the explore pipeline fail loudly (search-provider quota, synopsis rebuild), and add the `/canvas/search` (SSE, ephemeral candidates + streamed answer) and `/canvas/keep` (resolver-gated persist) surface to `master`.

**Architecture:** Spec: [`docs/plans/2026-07-17-canvas-v1-design.md`](2026-07-17-canvas-v1-design.md). **Premise correction vs the spec:** the June-2026 canvas build is unreachable (no `dev` ref exists on the remote; local `origin/dev` predates it) — this is a **fresh build on master's substrate**, implementing the same contract. Master already provides the two halves: `run_exploration` returns *unpersisted* candidates, and `resolve_and_persist` is the resolver-gated persist. Phase 1 adds a typed search-provider error, a gateway-routed + surfaced synopsis rebuild, a `CanvasConfig`, a streaming answer helper, and one new router.

**Tech Stack:** Python 3.11, FastAPI (SSE via `StreamingResponse`), pydantic v2, `openai` SDK against Vercel AI Gateway, tavily-python, SQLite (sqlite-vec) via the `Store` seam, pytest (`asyncio_mode=auto`).

## Global Constraints

- Repo: `/Users/anthonysuherli/Repositories/8star/delapan-ai/backend` (git repo `delapan-be`). Base branch: `master`. Do the work on a new branch `feat/canvas-v1-phase1` (worktree per superpowers:using-git-worktrees).
- Run tests as `.venv/bin/pytest` and lint as `.venv/bin/ruff check .` from `backend/`. Full suite must stay green; ruff line-length 100.
- Every non-`__init__` module starts with a docstring then `from __future__ import annotations`. Type hints throughout. Terse module docstrings with ASCII flow diagram (match existing files).
- Config invariant: every new knob lives in `AppConfig` + `config.yaml`, overridable via `DLP_<SECTION>__<FIELD>`. Never hardcode.
- `Store` protocol is the single persistence seam — no backend-specific objects cross it.
- Return shapes documented in this plan are contracts for phase 2 (frontend); don't rename fields ad hoc.
- Do not touch `delapan/core/exploration/extractor.py`'s per-page swallow or `narrator.py`'s swallow — partial-page and narration best-effort behavior is intentional. Only *total provider failure* becomes loud.

## File Structure

```
delapan/core/clients/tavily.py        MODIFY  TavilyError + raise-mode retry
delapan/core/agent/synopsis.py        MODIFY  gateway-first _build; maybe_rebuild_synopsis -> str
delapan/core/config.py                MODIFY  SynopsisConfig.model default; + CanvasConfig; AppConfig.canvas
config.yaml                           MODIFY  synopsis.model slug; + canvas: section
delapan/core/clients/ai_gateway.py    MODIFY  + stream_text_completion
delapan/core/canvas/__init__.py       CREATE  package exports
delapan/core/canvas/answer.py         CREATE  grounded streaming answer synthesis
delapan/api/deps.py                   MODIFY  + missing_pipeline_keys (moved from routes_explore)
delapan/api/routes_explore.py         MODIFY  use deps.missing_pipeline_keys; surface synopsis status
delapan/mcp/server.py                 MODIFY  surface synopsis status in delapan_explore result
delapan/api/routes_canvas.py          CREATE  POST /canvas/search (SSE) + POST /canvas/keep
delapan/api/main.py                   MODIFY  register canvas router
README.md                             MODIFY  "What's inside" + roadmap rows
tests/test_tavily_errors.py           CREATE
tests/test_explore_loud_failure.py    CREATE
tests/test_agent_synopsis.py          MODIFY  + routing/status tests
tests/test_config.py                  MODIFY  + canvas section test
tests/test_canvas_answer.py           CREATE
tests/test_api_canvas.py              CREATE
```

---

### Task 1: Typed Tavily errors — quota/auth failures raise instead of returning empty

**Files:**
- Modify: `delapan/core/clients/tavily.py`
- Test: `tests/test_tavily_errors.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `class TavilyError(RuntimeError)` in `delapan.core.clients.tavily`; `search(...)` and `extract(...)` now raise `TavilyError` on total provider failure. `extract` still returns partial results when ≥1 batch succeeds. Empty `urls` still returns `{}` without raising. Task 2 and Task 6 depend on `TavilyError` propagating out of `run_exploration`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tavily_errors.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tavily_errors.py -v`
Expected: FAIL — `ImportError: cannot import name 'TavilyError'`.

- [ ] **Step 3: Implement**

In `delapan/core/clients/tavily.py`:

3a. Add after `_EXTRACT_BATCH = 20`:

```python
class TavilyError(RuntimeError):
    """Search provider failed after retries (quota, auth, transport).

    Raised instead of returning an empty fallback so callers can mark the run
    failed — a provider outage must not masquerade as an empty web."""
```

3b. Replace `_with_retry` (keep the same decorator shape; `fallback=None` now means *raise*):

```python
def _with_retry(max_retries: int, base_delay: float, fallback: Callable[[], Any] | None = None):
    """Async exponential-backoff retry. Once exhausted: return ``fallback()`` if
    given, else raise ``TavilyError`` chained to the last provider error."""

    def decorator(func: Callable[..., Awaitable[Any]]):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — transport/provider errors
                    last_exc = exc
                    if attempt < max_retries:
                        await asyncio.sleep(base_delay * (2**attempt))
            logger.warning("%s exhausted retries: %s", func.__name__, last_exc)
            if fallback is not None:
                return fallback()
            raise TavilyError(
                f"{func.__name__} failed after {max_retries + 1} attempts: {last_exc}"
            ) from last_exc

        return wrapper

    return decorator
```

3c. Switch both call sites to raise-mode and make `extract` partial-tolerant:

```python
@_with_retry(max_retries=3, base_delay=1.0)
async def search(query: str, *, max_results: int, search_depth: str) -> list[dict]:
    """Run one Tavily search; returns the ranked ``results`` list.
    Raises ``TavilyError`` if the provider fails after retries."""
```
(body unchanged)

```python
async def extract(urls: list[str], *, search_depth: str = "advanced") -> dict[str, str]:
    """Fetch readable content for ``urls``. Returns ``{url: content}`` — URLs that
    return nothing are simply absent. Batched to Tavily's per-call cap. A failed
    batch is skipped when other batches succeed; if *every* batch fails the
    provider is down and ``TavilyError`` is raised."""
    if not urls:
        return {}
    extract_depth = "advanced" if search_depth == "advanced" else "basic"
    out: dict[str, str] = {}
    errors: list[TavilyError] = []
    for i in range(0, len(urls), _EXTRACT_BATCH):
        try:
            out.update(await _extract_batch(urls[i : i + _EXTRACT_BATCH], extract_depth))
        except TavilyError as exc:
            errors.append(exc)
    if errors and not out:
        raise errors[0]
    return out


@_with_retry(max_retries=2, base_delay=1.0)
async def _extract_batch(urls: list[str], extract_depth: str) -> dict[str, str]:
```
(`_extract_batch` body unchanged; only the decorator loses `fallback=dict`)

Also update the module docstring's claim about `[]`-on-failure to mention `TavilyError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tavily_errors.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Full suite + lint, then commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass (no existing test fakes Tavily, so nothing else moves).

```bash
git add delapan/core/clients/tavily.py tests/test_tavily_errors.py
git commit -m "feat(clients): raise TavilyError on total provider failure instead of silent empty"
```

---

### Task 2: Loud-failure propagation — provider error ⇒ run `failed`, SSE error frame

No production code is expected to change here: `run_exploration` has no catch between the search call and its outer re-raising `except` (`engine.py:130-132`), and both call sites already mark the row `failed` on exception. This task pins that chain with tests so it can't regress.

**Files:**
- Test: `tests/test_explore_loud_failure.py` (create)

**Interfaces:**
- Consumes: `TavilyError` (Task 1); `run_exploration` from `delapan.core.exploration`; `ExplorationPlan`, `SearchQuery` from `delapan.core.exploration.models`.
- Produces: nothing new — regression net only.

- [ ] **Step 1: Write the tests**

Create `tests/test_explore_loud_failure.py`:

```python
"""Provider failure must surface: engine re-raises, run row goes `failed`,
the explore SSE stream emits a terminal error frame. Pins the loud-failure
chain introduced with TavilyError (canvas v1 phase 1)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from delapan.core.clients import tavily as tavily_mod
from delapan.core.clients.tavily import TavilyError
from delapan.core.exploration import engine as engine_mod
from delapan.core.exploration.models import ExplorationPlan, SearchQuery


@pytest.fixture()
def quiet_narration(monkeypatch):
    monkeypatch.setenv("DLP_NARRATION__ENABLED", "false")
    from delapan.core.config import get_config

    get_config.cache_clear()
    yield
    get_config.cache_clear()


async def test_run_exploration_propagates_tavily_error(monkeypatch, quiet_narration):
    async def _fake_plan(prompt, cfg, lens="explore"):
        return ExplorationPlan(
            search_queries=[SearchQuery(query="q")],
            extraction_prompt="",
            expected_categories=[],
            finding_title_hint="",
        )

    async def _boom(query, *, max_results, search_depth):
        raise TavilyError("search failed after 4 attempts: 432 quota")

    monkeypatch.setattr(engine_mod, "plan_queries", _fake_plan)
    monkeypatch.setattr(tavily_mod, "search", _boom)

    phases: list[str] = []

    async def on_progress(phase: str) -> None:
        phases.append(phase)

    from delapan.core.config import get_config

    with pytest.raises(TavilyError):
        await engine_mod.run_exploration(
            "topic",
            exploration_id="e1",
            project_id="p1",
            kb_id="k1",
            cfg=get_config().exploration,
            on_progress=on_progress,
        )
    assert any(p.startswith("error") for p in phases)
    assert "completed" not in phases


def test_explore_route_marks_row_failed_and_emits_error_frame(
    tmp_path, monkeypatch, client, kb, quiet_narration
):
    # Keys must pass the guard so the pipeline (monkeypatched to fail) is reached.
    for key in ("TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()

    async def _boom(prompt, **kwargs):
        raise TavilyError("search failed after 4 attempts: 432 quota")

    from delapan.api import routes_explore as routes_mod

    monkeypatch.setattr(routes_mod, "run_exploration", _boom)

    r = client.post("/api/projects/proj/kbs/kb/explore", json={"prompt": "x"})
    assert r.status_code == 200
    assert '"phase": "error"' in r.text and "432 quota" in r.text

    rows = sqlite3.connect(str(tmp_path / "api.db")).execute(
        "select status, error from explorations"
    ).fetchall()
    assert len(rows) == 1
    status, error = rows[0]
    assert status == "failed" and "432 quota" in error
```

The `client` / `kb` fixtures don't exist in this file — import-reuse them by copying the two fixtures verbatim from `tests/test_api_routes.py:9-33` into this file (they are 25 lines; the repo has no shared conftest entry for them, and cross-file fixture imports are not a pattern here).

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/test_explore_loud_failure.py -v`
Expected: PASS both (behavior already correct after Task 1; if either fails, the chain has a real hole — fix the production code, not the test).

- [ ] **Step 3: Commit**

```bash
git add tests/test_explore_loud_failure.py
git commit -m "test(explore): pin loud-failure chain — provider error marks run failed, SSE emits error"
```

---

### Task 3: Synopsis rebuild — gateway-first routing + surfaced status

Root cause of the silent breakage: `synopsis._build` always uses the direct-Anthropic client (`chat_model`) while the rest of the pipeline runs on the AI Gateway; in a gateway-only `.env` it fails on every rebuild, swallowed by a broad `except`. Fix: route by model-slug shape (`provider/model` ⇒ gateway `text_completion`, bare slug ⇒ direct Anthropic), preflight the needed key with a clear error, and return a status string that both explore surfaces report.

**Files:**
- Modify: `delapan/core/agent/synopsis.py`
- Modify: `delapan/core/config.py` (SynopsisConfig.model default, line ~205)
- Modify: `config.yaml` (synopsis.model, line ~58)
- Modify: `delapan/mcp/server.py` (delapan_explore return dict)
- Modify: `delapan/api/routes_explore.py` (`_run_and_persist` result dict)
- Test: `tests/test_agent_synopsis.py` (add tests; file exists)

**Interfaces:**
- Consumes: `text_completion(*, model, system, user, temperature=0.0, max_tokens=None) -> str` from `delapan.core.clients.ai_gateway` (exists); `chat_model`, `text_of` from `delapan.core.clients.anthropic` (exist).
- Produces: `maybe_rebuild_synopsis(kb_id, *, org_id=None, store=None) -> str` returning `"rebuilt" | "skipped" | "failed: <msg>"` (was `-> None`; still never raises). `delapan_explore` result gains `"synopsis": <status>`; the explore SSE `completed` frame gains the same key. Task 7's keep route reuses both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_synopsis.py` (match its existing imports/style; add any missing imports at top):

```python
from delapan.core.agent import synopsis as synopsis_mod
from delapan.core.config import SynopsisConfig


async def test_build_routes_gateway_slug_through_text_completion(monkeypatch):
    seen = {}

    async def _fake_text_completion(*, model, system, user, temperature=0.0, max_tokens=None):
        seen["model"] = model
        return '[{"topic": "t", "gloss": "g"}]'

    monkeypatch.setattr(synopsis_mod, "text_completion", _fake_text_completion)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()

    out = await synopsis_mod._build(
        [{"title": "A", "category": "c"}], SynopsisConfig(model="anthropic/claude-haiku-4.5")
    )
    assert seen["model"] == "anthropic/claude-haiku-4.5"
    assert out == [{"topic": "t", "gloss": "g"}]
    get_settings.cache_clear()


async def test_build_gateway_slug_without_key_raises_actionable(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc_info:
        await synopsis_mod._build([{"title": "A"}], SynopsisConfig(model="anthropic/claude-haiku-4.5"))
    assert "AI_GATEWAY_API_KEY" in str(exc_info.value)
    get_settings.cache_clear()


async def test_direct_slug_without_anthropic_key_raises_actionable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from delapan.core.config import get_settings

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
```

(`store.insert_findings` is async — `resolve_and_persist` awaits it — hence the `await` above.)

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/test_agent_synopsis.py -v`
Expected: new tests FAIL (`text_completion` not an attribute of synopsis module; `maybe_rebuild_synopsis` returns `None`).

- [ ] **Step 3: Implement `synopsis.py` changes**

3a. Imports — add `text_completion` and `text_of`, keep `chat_model`:

```python
from delapan.core.clients.ai_gateway import text_completion
from delapan.core.clients.anthropic import chat_model, text_of
from delapan.core.config import SynopsisConfig, get_config, get_settings
```

3b. Replace `_build` with slug-routed version:

```python
_SYNOPSIS_SYSTEM = "You produce compact JSON knowledge-base synopses. Return ONLY JSON."


async def _synopsis_text(cfg: SynopsisConfig, prompt: str) -> str:
    """One completion for the synopsis, routed by model-slug shape: gateway
    slugs (``provider/model``) go through the AI Gateway; bare slugs use the
    direct Anthropic client. Missing keys raise with the exact env var to set."""
    s = get_settings()
    if "/" in cfg.model:
        if not s.ai_gateway_api_key:
            raise RuntimeError(
                f"synopsis model {cfg.model!r} routes via AI Gateway — set AI_GATEWAY_API_KEY"
            )
        return await text_completion(model=cfg.model, system=_SYNOPSIS_SYSTEM, user=prompt)
    if not s.anthropic_api_key:
        raise RuntimeError(
            f"synopsis model {cfg.model!r} is a direct Anthropic slug — set ANTHROPIC_API_KEY"
        )
    llm = chat_model(cfg.model)
    resp = await llm.ainvoke([{"role": "user", "content": prompt}])
    return resp.content if isinstance(resp.content, str) else text_of(resp.content)


async def _build(findings: list[dict], cfg: SynopsisConfig) -> list[dict]:
    text = await _synopsis_text(cfg, _build_prompt(findings, cfg))
    try:
        data = json.loads(text[text.find("[") : text.rfind("]") + 1])
        # Load-bearing: the dict-filter keeps the [:max_entries] slice safe — a
        # mis-sliced non-dict list (e.g. a parsed JSON object) degrades to [].
        return [e for e in data if isinstance(e, dict)][: cfg.max_entries]
    except (ValueError, json.JSONDecodeError):
        logger.warning("synopsis JSON parse failed; len=%d", len(text))
        return []
```

3c. `maybe_rebuild_synopsis` returns a status (docstring: keep "never raises", document the three statuses):

```python
async def maybe_rebuild_synopsis(
    kb_id: str, *, org_id: str | None = None, store: Store | None = None
) -> str:
    """Rebuild the synopsis if the KB grew enough. Never raises.

    Returns ``"rebuilt"``, ``"skipped"`` (thresholds not met), or
    ``"failed: <msg>"`` — callers surface the string so a broken rebuild is
    visible instead of silent. `org_id` is accepted for signature parity."""
    try:
        cfg = get_config().synopsis
        store = store or get_store()
        live_count = store.count_findings(kb_id)
        row = store.load_synopsis(kb_id)
        if not should_rebuild(live_count, row, cfg):
            return "skipped"
        listing = store.list_findings(kb_id, limit=200)
        rows = listing.get("findings", []) if isinstance(listing, dict) else []
        findings = [f for f in rows if isinstance(f, dict)]
        content = await _build(findings, cfg)
        store.upsert_synopsis(kb_id, content=content, finding_count=live_count, model=cfg.model)
        return "rebuilt"
    except Exception as exc:  # noqa: BLE001 — regen is best-effort, never breaks a turn
        logger.exception("synopsis rebuild failed for kb=%s", kb_id)
        return f"failed: {exc}"
```

(`schedule_rebuild` needs no change — it ignores the return value.)

- [ ] **Step 4: Flip the default model slug to the gateway**

`delapan/core/config.py` — `SynopsisConfig` (line ~202-208): change `model: str = "claude-haiku-4-5"` to:

```python
    model: str = "anthropic/claude-haiku-4.5"  # gateway slug; bare slug ⇒ direct Anthropic
```

`config.yaml` synopsis section (line ~58): change `model: claude-haiku-4-5` to `model: anthropic/claude-haiku-4.5`.

- [ ] **Step 5: Surface the status at both call sites**

`delapan/mcp/server.py` (`delapan_explore`, lines ~114-126) — capture and return:

```python
        syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise
    return {
        "exploration_id": exp_id,
        "finding_ids": ids,
        "count": len(ids),
        "synopsis": syn_status,
    }
```

Update the tool docstring's return-shape line to include `"synopsis"`.

`delapan/api/routes_explore.py` (`_run_and_persist`, lines ~93-99) — same capture; final line becomes:

```python
        syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — mark the row failed, then re-raise
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso(), error=str(exc))
        raise

    return {"finding_ids": ids, "count": len(ids), "synopsis": syn_status}
```

(The `completed` SSE frame spreads this dict, so the frame gains the key automatically. Update the module docstring's frame diagram.)

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_agent_synopsis.py tests/test_api_routes.py tests/test_mcp_smoke.py -v`
Expected: PASS. If an existing test asserts the exact `delapan_explore` / completed-frame dict shape, extend its expectation with the `"synopsis"` key.

- [ ] **Step 7: Full suite + lint, commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`

```bash
git add delapan/core/agent/synopsis.py delapan/core/config.py config.yaml \
  delapan/mcp/server.py delapan/api/routes_explore.py tests/test_agent_synopsis.py
git commit -m "feat(synopsis): gateway-first rebuild routing + surfaced rebuild status"
```

---

### Task 4: `CanvasConfig` — the new knobs

**Files:**
- Modify: `delapan/core/config.py` (new section class + `AppConfig` field)
- Modify: `config.yaml` (new `canvas:` section)
- Test: `tests/test_config.py` (add one test)

**Interfaces:**
- Produces: `get_config().canvas` with fields `answer_model: str`, `answer_max_tokens: int`, `max_candidates: int`, `keep_max_candidates: int`, `keep_max_content_chars: int`, `max_history_turns: int`. Tasks 5-7 consume it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (follow its existing override-test pattern — it uses `monkeypatch.setenv` + `get_config.cache_clear()`):

```python
def test_canvas_section_defaults_and_env_override(monkeypatch):
    from delapan.core.config import get_config

    get_config.cache_clear()
    cfg = get_config().canvas
    assert cfg.answer_model == "anthropic/claude-sonnet-4.6"
    assert cfg.max_candidates == 24
    assert cfg.keep_max_candidates == 20
    assert cfg.keep_max_content_chars == 8000
    assert cfg.max_history_turns == 8
    assert cfg.answer_max_tokens == 1024

    monkeypatch.setenv("DLP_CANVAS__MAX_CANDIDATES", "5")
    get_config.cache_clear()
    assert get_config().canvas.max_candidates == 5
    get_config.cache_clear()
```

- [ ] **Step 2: Run it — expect FAIL** (`AppConfig` has no `canvas`).

Run: `.venv/bin/pytest tests/test_config.py -v`

- [ ] **Step 3: Implement**

`delapan/core/config.py` — add after `MemoryConfig` (line ~389):

```python
class CanvasConfig(BaseModel):
    """Search-canvas surface: ephemeral web search + keep-gated persistence.
    The answer model is an AI Gateway slug; caps bound what one request can
    stream (candidates) and what one keep may write (count + content chars)."""

    answer_model: str = "anthropic/claude-sonnet-4.6"
    answer_max_tokens: int = 1024
    max_candidates: int = 24  # candidates surfaced per search
    keep_max_candidates: int = 20  # write boundary: max kept per /canvas/keep
    keep_max_content_chars: int = 8000  # per-candidate clamp on content strings
    max_history_turns: int = 8  # chat-thread messages replayed into synthesis
```

`AppConfig` — add field (alphabetically near the top of the list, matching the section-per-line style):

```python
    canvas: CanvasConfig = Field(default_factory=CanvasConfig)
```

`config.yaml` — add a `canvas:` section (mirror the code defaults, commented like sibling sections; place it after `synopsis:`):

```yaml
# Search-canvas surface — ephemeral search + keep-gated persistence.
canvas:
  answer_model: anthropic/claude-sonnet-4.6
  answer_max_tokens: 1024
  max_candidates: 24
  keep_max_candidates: 20
  keep_max_content_chars: 8000
  max_history_turns: 8
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `.venv/bin/pytest tests/test_config.py -q && .venv/bin/pytest -q && .venv/bin/ruff check .`

```bash
git add delapan/core/config.py config.yaml tests/test_config.py
git commit -m "feat(config): CanvasConfig section — answer model + candidate/keep caps"
```

---

### Task 5: Streaming answer synthesis — `stream_text_completion` + `core/canvas/answer.py`

**Files:**
- Modify: `delapan/core/clients/ai_gateway.py` (add `stream_text_completion`)
- Create: `delapan/core/canvas/__init__.py`, `delapan/core/canvas/answer.py`
- Test: `tests/test_canvas_answer.py` (create)

**Interfaces:**
- Consumes: `gateway_client()` (exists), `CanvasConfig` (Task 4).
- Produces:
  - `stream_text_completion(*, model: str, system: str, messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None) -> AsyncIterator[str]` in `ai_gateway` — yields text deltas; `messages` are `{"role": "user"|"assistant", "content": str}` dicts appended after the system message.
  - `build_answer_messages(prompt: str, history: list[dict], cfg: CanvasConfig) -> list[dict]` and `stream_answer(prompt: str, *, preamble_xml: str, candidates: list[Finding], history: list[dict], cfg: CanvasConfig) -> AsyncIterator[str]` in `delapan.core.canvas.answer`. Task 6's route consumes `stream_answer`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_canvas_answer.py`:

```python
"""Answer synthesis: grounded system prompt + clamped history + streamed deltas."""

from __future__ import annotations

from datetime import datetime, timezone

from delapan.core.canvas import answer as answer_mod
from delapan.core.config import CanvasConfig
from delapan.core.exploration.models import Finding


def _candidate(title: str) -> Finding:
    now = datetime.now(timezone.utc)
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
```

- [ ] **Step 2: Run — expect FAIL** (`delapan.core.canvas` doesn't exist).

Run: `.venv/bin/pytest tests/test_canvas_answer.py -v`

- [ ] **Step 3: Implement `stream_text_completion`**

Append to `delapan/core/clients/ai_gateway.py` (after `text_completion`):

```python
async def stream_text_completion(
    *,
    model: str,
    system: str,
    messages: list[dict],
    temperature: float = 0.2,
    max_tokens: int | None = None,
):
    """Streamed plain-text completion through AI Gateway — yields text deltas.

    ``messages`` are ``{"role", "content"}`` turns appended after the system
    message (a chat thread). Structured output and fallback models don't apply
    here; a transport/provider failure raises to the caller mid-stream."""
    stream = await gateway_client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, *messages],
        temperature=temperature,
        max_tokens=max_tokens if max_tokens is not None else omit,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta
```

- [ ] **Step 4: Implement the canvas answer module**

Create `delapan/core/canvas/__init__.py`:

```python
"""Search-canvas core: grounded answer synthesis over ephemeral candidates."""

from delapan.core.canvas.answer import stream_answer

__all__ = ["stream_answer"]
```

Create `delapan/core/canvas/answer.py`:

```python
"""Grounded streaming answer for the canvas search thread.

    preamble (KB) + candidates (fresh web) ─► system prompt
    history + prompt ─────────────────────► messages
                └─► stream_text_completion ─► text deltas

The KB preamble grounds the reply in what the user already keeps; candidates
ground it in what this search just surfaced. Persistence is elsewhere (/keep).
"""

from __future__ import annotations

from typing import AsyncIterator

from delapan.core.clients.ai_gateway import stream_text_completion
from delapan.core.config import CanvasConfig
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import render_content

_SYSTEM_TEMPLATE = (
    "You are the research companion on a knowledge-canvas. Answer the user's "
    "question concisely from the grounding below. Prefer candidate findings for "
    "fresh facts and the KB preamble for what the user already knows; name "
    "candidate titles when you draw on them. If the grounding is insufficient, "
    "say so plainly.\n\n"
    "## KB preamble\n{preamble}\n\n## Candidate findings (this search)\n{candidates}"
)


def _candidate_digest(candidates: list[Finding], *, char_budget: int = 700) -> str:
    blocks = []
    for i, f in enumerate(candidates):
        body = render_content(f.content)[:char_budget]
        blocks.append(f"[{i}] {f.title} ({f.category})\n{body}")
    return "\n\n".join(blocks) if blocks else "(none)"


def build_answer_messages(prompt: str, history: list[dict], cfg: CanvasConfig) -> list[dict]:
    """Thread messages for synthesis: clamped history then the new user turn."""
    clamped = [
        {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
        for m in history[-cfg.max_history_turns :]
    ]
    return [*clamped, {"role": "user", "content": prompt}]


async def stream_answer(
    prompt: str,
    *,
    preamble_xml: str,
    candidates: list[Finding],
    history: list[dict],
    cfg: CanvasConfig,
) -> AsyncIterator[str]:
    """Yield answer text deltas grounded in the KB preamble + candidates."""
    system = _SYSTEM_TEMPLATE.format(
        preamble=preamble_xml, candidates=_candidate_digest(candidates)
    )
    async for delta in stream_text_completion(
        model=cfg.answer_model,
        system=system,
        messages=build_answer_messages(prompt, history, cfg),
        max_tokens=cfg.answer_max_tokens,
    ):
        yield delta
```

**Check before coding:** `render_content` lives in `delapan/core/exploration/render.py` (it renders a finding `content` dict to markdown; `resolve_and_persist` imports it — copy the exact import path from `delapan/core/memory/persist.py`). If its name/path differs, use persist.py's import verbatim.

- [ ] **Step 5: Run tests, full suite, commit**

Run: `.venv/bin/pytest tests/test_canvas_answer.py -q && .venv/bin/pytest -q && .venv/bin/ruff check .`

```bash
git add delapan/core/clients/ai_gateway.py delapan/core/canvas tests/test_canvas_answer.py
git commit -m "feat(canvas): grounded streaming answer synthesis over gateway"
```

---

### Task 6: `POST /canvas/search` — SSE stream of progress, candidates, answer

**Files:**
- Modify: `delapan/api/deps.py` (add `missing_pipeline_keys`, moved from routes_explore)
- Modify: `delapan/api/routes_explore.py` (import it; delete local `_missing_keys`)
- Create: `delapan/api/routes_canvas.py`
- Test: `tests/test_api_canvas.py` (create)

**Interfaces:**
- Consumes: `resolve_kb_or_404` (deps), `run_exploration`, `select_preamble(query, *, store, kb_id, depth) -> tuple[str, Coverage]` (`delapan.core.agent.preamble`), `stream_answer` (Task 5), `get_config().canvas` (Task 4), store methods `create_exploration` / `update_exploration`.
- Produces: SSE contract (phase 2 frontend consumes this verbatim):
  - `{"phase": "grounding", "coverage": "rich"|"sparse"|"gap"}` — first frame
  - `{"phase": "planning"|"searching"|"crawling"|"extracting"|"merging", "detail": null}`
  - `{"phase": "candidates", "exploration_id": str, "candidates": [{id, category, title, content, confidence, tags, provenance}]}`
  - `{"phase": "answer", "delta": str}` (repeated)
  - `{"phase": "completed", "candidate_count": int}` — terminal
  - `{"phase": "error", "error": str}` — terminal (missing keys, provider failure, or synthesis failure)
  - Also produces `missing_pipeline_keys() -> list[str]` in `delapan.api.deps` for reuse.

- [ ] **Step 1: Move the key guard into deps**

In `delapan/api/deps.py`, add (imports: `get_settings` from `delapan.core.config`):

```python
def missing_pipeline_keys() -> list[str]:
    """Credentials the research pipeline needs end-to-end (search + LLM + embeddings)."""
    s = get_settings()
    required = (
        ("TAVILY_API_KEY", s.tavily_api_key),
        ("AI_GATEWAY_API_KEY", s.ai_gateway_api_key),
        ("OPENAI_API_KEY", s.openai_api_key),
    )
    return [name for name, value in required if not value]
```

In `routes_explore.py`: delete `_missing_keys` (lines ~54-62), import `missing_pipeline_keys` from `delapan.api.deps`, and replace the one call site (`missing = _missing_keys()` → `missing = missing_pipeline_keys()`).

Run: `.venv/bin/pytest tests/test_api_routes.py -q` — the existing missing-keys SSE test must still pass.

- [ ] **Step 2: Write the failing route tests**

Create `tests/test_api_canvas.py` (copy the `client` / `kb` fixtures verbatim from `tests/test_api_routes.py:9-33`, plus `BASE = "/api/projects/proj/kbs/kb"`):

```python
"""Canvas surface: /canvas/search streams candidates+answer without persisting;
/canvas/keep persists through the resolver and returns its events."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from delapan.core.exploration.models import Finding

# ... client / kb fixtures + BASE copied from tests/test_api_routes.py ...


def _frames(sse_text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in sse_text.splitlines()
        if line.startswith("data: ")
    ]


def _candidate(title: str) -> Finding:
    now = datetime.now(timezone.utc)
    return Finding(
        exploration_id="e1",
        project_id="p1",
        category="cat",
        title=title,
        content={"k": "v"},
        confidence=0.4,
        provenance=[{"url": f"http://src/{title}"}],
        created_at=now,
        updated_at=now,
    )


def test_canvas_search_missing_keys_emits_sse_error(client, kb):
    r = client.post(f"{BASE}/canvas/search", json={"prompt": "agent memory"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    assert frames[-1]["phase"] == "error" and "missing required keys" in frames[-1]["error"]


def test_canvas_search_unknown_kb_is_404(client):
    assert client.post("/api/projects/nope/kbs/kb/canvas/search", json={"prompt": "x"}).status_code == 404


def test_canvas_search_streams_candidates_and_answer_without_persisting(
    client, kb, monkeypatch
):
    store, kb_id = kb
    for key in ("TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()

    from delapan.api import routes_canvas as canvas_mod

    async def _fake_run(prompt, *, exploration_id, project_id, kb_id, cfg, on_progress=None, **kw):
        if on_progress:
            await on_progress("planning")
            await on_progress("searching")
        return [_candidate("A"), _candidate("B")]

    async def _fake_preamble(query, *, store, kb_id, depth="shallow"):
        return "<preamble><empty/></preamble>", "gap"

    async def _fake_answer(prompt, *, preamble_xml, candidates, history, cfg):
        for chunk in ["Hello ", "world"]:
            yield chunk

    monkeypatch.setattr(canvas_mod, "run_exploration", _fake_run)
    monkeypatch.setattr(canvas_mod, "select_preamble", _fake_preamble)
    monkeypatch.setattr(canvas_mod, "stream_answer", _fake_answer)

    r = client.post(f"{BASE}/canvas/search", json={"prompt": "quota limits"})
    assert r.status_code == 200
    frames = _frames(r.text)
    phases = [f["phase"] for f in frames]

    assert phases[0] == "grounding" and frames[0]["coverage"] == "gap"
    assert "planning" in phases and "searching" in phases
    cand = next(f for f in frames if f["phase"] == "candidates")
    assert [c["title"] for c in cand["candidates"]] == ["A", "B"]
    assert cand["exploration_id"]
    assert [f["delta"] for f in frames if f["phase"] == "answer"] == ["Hello ", "world"]
    assert frames[-1] == {"phase": "completed", "candidate_count": 2}

    # Ephemeral: nothing persisted, but the run row exists and is completed-empty.
    assert store.count_findings(kb_id) == 0
    import sqlite3

    rows = sqlite3.connect(store_db_path(client)).execute(
        "select status, finding_ids from explorations"
    ).fetchall()
    assert rows[0][0] == "completed" and json.loads(rows[0][1]) == []
```

For the sqlite assertion, define at top of the file (the client fixture pins the DB path):

```python
import os


def store_db_path(_client) -> str:
    return os.environ["DELAPAN_DB_PATH"]
```

Add one more test — pipeline failure surfaces:

```python
def test_canvas_search_provider_failure_emits_error_and_fails_row(client, kb, monkeypatch):
    for key in ("TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fake")
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    from delapan.api import routes_canvas as canvas_mod
    from delapan.core.clients.tavily import TavilyError

    async def _fake_preamble(query, *, store, kb_id, depth="shallow"):
        return "<preamble><empty/></preamble>", "gap"

    async def _boom(prompt, **kwargs):
        raise TavilyError("search failed after 4 attempts: 432 quota")

    monkeypatch.setattr(canvas_mod, "select_preamble", _fake_preamble)
    monkeypatch.setattr(canvas_mod, "run_exploration", _boom)

    r = client.post(f"{BASE}/canvas/search", json={"prompt": "x"})
    frames = _frames(r.text)
    assert frames[-1]["phase"] == "error" and "432 quota" in frames[-1]["error"]
    import json as _json
    import sqlite3

    rows = sqlite3.connect(store_db_path(client)).execute(
        "select status, error from explorations"
    ).fetchall()
    assert rows[0][0] == "failed" and "432 quota" in rows[0][1]
```

- [ ] **Step 3: Run — expect FAIL** (no module `delapan.api.routes_canvas`).

Run: `.venv/bin/pytest tests/test_api_canvas.py -v`

- [ ] **Step 4: Implement the router (search half)**

Create `delapan/api/routes_canvas.py`:

```python
"""Canvas routes — ephemeral search + keep-gated persistence.

    POST /api/projects/{p}/kbs/{k}/canvas/search   {"prompt", "history"?, "max_candidates"?}
        │  SSE: grounding → planning…merging → candidates → answer* → completed|error
        │  (runs the pipeline with NO persistence — candidates are ephemeral)
    POST /api/projects/{p}/kbs/{k}/canvas/keep     {"candidates": [...]}
        │  persists kept candidates through the memory resolver
        └─► {"finding_ids", "events", "synopsis"}

Search is browse-only; `/keep` is the explicit HITL persistence gate, routed
through ``resolve_and_persist`` (ADD/UPDATE/NOOP/SUPERSEDE) — its events are
the frontend's graph-delta + character feed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncIterator, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from delapan.api.deps import missing_pipeline_keys, resolve_kb_or_404
from delapan.core.agent.preamble import select_preamble
from delapan.core.agent.state import TenantContext
from delapan.core.agent.synopsis import maybe_rebuild_synopsis
from delapan.core.canvas.answer import stream_answer
from delapan.core.config import get_config
from delapan.core.exploration import run_exploration
from delapan.core.exploration.models import Finding
from delapan.core.knowledge_graph.builder import schedule_kg_update
from delapan.core.memory.persist import resolve_and_persist
from delapan.store import Store

router = APIRouter(prefix="/api/projects/{project}/kbs/{kb}")


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CanvasSearchBody(BaseModel):
    prompt: str
    history: list[ChatTurn] = Field(default_factory=list)
    max_candidates: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


_CANDIDATE_FIELDS = {"id", "category", "title", "content", "confidence", "tags", "provenance"}


async def _search_events(
    ctx: TenantContext, store: Store, body: CanvasSearchBody
) -> AsyncIterator[str]:
    missing = missing_pipeline_keys()
    if missing:
        yield _sse({"phase": "error", "error": f"missing required keys: {', '.join(missing)}"})
        return

    cfg = get_config()
    ccfg = cfg.canvas
    cap = min(body.max_candidates or ccfg.max_candidates, ccfg.max_candidates)

    preamble_xml, coverage = await select_preamble(
        body.prompt, store=store, kb_id=ctx.kb_id, depth="shallow"
    )
    yield _sse({"phase": "grounding", "coverage": coverage})

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    result: dict = {}

    async def on_progress(phase: str) -> None:
        if phase == "completed" or phase.startswith("error"):
            return
        await queue.put({"phase": phase, "detail": None})

    async def run() -> None:
        exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, body.prompt)
        try:
            findings = await run_exploration(
                body.prompt,
                exploration_id=exp_id,
                project_id=ctx.project_id,
                kb_id=ctx.kb_id,
                cfg=cfg.exploration,
                on_progress=on_progress,
            )
            # Browse-only: record the run, persist nothing.
            store.update_exploration(
                exp_id, status="completed", completed_at=_now_iso(), finding_ids=[]
            )
            result["exploration_id"] = exp_id
            result["candidates"] = findings[:cap]
        except Exception as exc:  # noqa: BLE001 — mark the row failed, surface as SSE error
            store.update_exploration(
                exp_id, status="failed", completed_at=_now_iso(), error=str(exc)
            )
            result["error"] = str(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _sse(item)

        if "error" in result:
            yield _sse({"phase": "error", "error": result["error"]})
            return

        candidates: list[Finding] = result["candidates"]
        yield _sse(
            {
                "phase": "candidates",
                "exploration_id": result["exploration_id"],
                "candidates": [f.model_dump(include=_CANDIDATE_FIELDS) for f in candidates],
            }
        )

        try:
            async for delta in stream_answer(
                body.prompt,
                preamble_xml=preamble_xml,
                candidates=candidates,
                history=[t.model_dump() for t in body.history],
                cfg=ccfg,
            ):
                yield _sse({"phase": "answer", "delta": delta})
        except Exception as exc:  # noqa: BLE001 — candidates already delivered; surface and stop
            yield _sse({"phase": "error", "error": f"answer synthesis failed: {exc}"})
            return

        yield _sse({"phase": "completed", "candidate_count": len(candidates)})
    finally:
        if not task.done():
            task.cancel()


@router.post("/canvas/search")
async def canvas_search(project: str, kb: str, body: CanvasSearchBody) -> StreamingResponse:
    ctx, store = resolve_kb_or_404(project, kb)
    return StreamingResponse(_search_events(ctx, store, body), media_type="text/event-stream")
```

Register in `delapan/api/main.py`: add `from delapan.api.routes_canvas import router as canvas_router` to the import block and `app.include_router(canvas_router)` after the explore router; add the two canvas lines to the module docstring's route diagram.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_api_canvas.py -v`
Expected: the search tests PASS (keep tests arrive in Task 7).

- [ ] **Step 6: Full suite + lint, commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`

```bash
git add delapan/api/deps.py delapan/api/routes_explore.py delapan/api/routes_canvas.py \
  delapan/api/main.py tests/test_api_canvas.py
git commit -m "feat(api): POST /canvas/search — SSE grounding/progress/candidates/answer, no persistence"
```

---

### Task 7: `POST /canvas/keep` — resolver-gated persistence

**Files:**
- Modify: `delapan/api/routes_canvas.py`
- Test: `tests/test_api_canvas.py` (extend)

**Interfaces:**
- Consumes: `resolve_and_persist(ctx, store, candidates: list[Finding], cfg) -> ResolutionOutcome` (exists); `maybe_rebuild_synopsis -> str` (Task 3); `schedule_kg_update` (exists); `CanvasConfig` caps (Task 4).
- Produces (phase-2/3 contract — the character's event feed):
  - Request: `{"candidates": [{category, title, content, confidence?, tags?, provenance?}]}` — server clamps count to `canvas.keep_max_candidates` and each content string value to `canvas.keep_max_content_chars`.
  - Response: `{"finding_ids": [...], "events": [{"op": "ADD"|"UPDATE"|"NOOP"|"SUPERSEDE", "candidate_title", "target_finding_id", "new_finding_id", "details", "reason"}], "synopsis": "<status>"}` (events are `ResolutionEvent.model_dump()` verbatim; `new_finding_id` = the row that now represents the knowledge, `target_finding_id` = the prior row it corroborated/updated/retired).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_canvas.py`:

```python
@pytest.fixture()
def keep_env(monkeypatch):
    """Keep path needs embeddings faked (no real keys) and memory enabled."""

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    from delapan.core.memory import persist as persist_mod

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    monkeypatch.setenv("DLP_MEMORY__ENABLED", "true")
    from delapan.core.config import get_config

    get_config.cache_clear()
    yield persist_mod
    get_config.cache_clear()


def _keep_payload(titles: list[str]) -> dict:
    return {
        "candidates": [
            {
                "category": "cat",
                "title": t,
                "content": {"k": f"fact about {t}"},
                "confidence": 0.4,
                "tags": [],
                "provenance": [{"url": f"http://src/{t}"}],
            }
            for t in titles
        ]
    }


def test_keep_persists_through_resolver_and_returns_events(client, kb, keep_env, monkeypatch):
    store, kb_id = kb
    persist_mod = keep_env

    from delapan.core.memory.models import ResolutionDecision, ResolutionOp

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)

    r = client.post(f"{BASE}/canvas/keep", json=_keep_payload(["A", "B"]))
    assert r.status_code == 200
    data = r.json()
    assert len(data["finding_ids"]) == 2
    assert [e["op"] for e in data["events"]] == ["ADD", "ADD"]
    assert all(e["new_finding_id"] for e in data["events"])
    assert "synopsis" in data
    assert store.count_findings(kb_id) == 2


def test_rekeep_noop_produces_no_duplicates(client, kb, keep_env, monkeypatch):
    store, kb_id = kb
    persist_mod = keep_env

    from delapan.core.memory.models import ResolutionDecision, ResolutionOp

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)
    first = client.post(f"{BASE}/canvas/keep", json=_keep_payload(["A"])).json()
    fid = first["finding_ids"][0]

    async def _noop(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(candidate_index=0, op=ResolutionOp.NOOP, target_finding_id=fid)
        ]

    monkeypatch.setattr(persist_mod, "resolve", _noop)
    second = client.post(f"{BASE}/canvas/keep", json=_keep_payload(["A"])).json()
    assert second["finding_ids"] == []
    assert [e["op"] for e in second["events"]] == ["NOOP"]
    assert store.count_findings(kb_id) == 1


def test_keep_clamps_count_and_content(client, kb, keep_env, monkeypatch):
    store, kb_id = kb
    persist_mod = keep_env
    seen = {}

    from delapan.core.memory.models import ResolutionDecision, ResolutionOp

    async def _spy_resolve(store_, kb_, cands, embs, mcfg):
        seen["cands"] = cands
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _spy_resolve)
    monkeypatch.setenv("DLP_CANVAS__KEEP_MAX_CANDIDATES", "2")
    monkeypatch.setenv("DLP_CANVAS__KEEP_MAX_CONTENT_CHARS", "10")
    from delapan.core.config import get_config

    get_config.cache_clear()

    payload = _keep_payload(["A", "B", "C"])
    payload["candidates"][0]["content"] = {"k": "x" * 50}
    r = client.post(f"{BASE}/canvas/keep", json=payload)
    assert r.status_code == 200
    assert len(seen["cands"]) == 2                       # count clamped
    assert seen["cands"][0].content == {"k": "x" * 10}   # content strings clamped
    get_config.cache_clear()


def test_keep_empty_candidates_is_400(client, kb):
    r = client.post(f"{BASE}/canvas/keep", json={"candidates": []})
    assert r.status_code == 400
```

- [ ] **Step 2: Run — expect FAIL** (no `/canvas/keep` route → 404/405).

Run: `.venv/bin/pytest tests/test_api_canvas.py -v`

- [ ] **Step 3: Implement the keep half**

Append to `delapan/api/routes_canvas.py` (add `HTTPException` to the fastapi import):

```python
class CandidateIn(BaseModel):
    category: str = ""
    title: str
    content: dict
    confidence: float = 0.0
    tags: list[str] = Field(default_factory=list)
    provenance: list[dict] = Field(default_factory=list)


class KeepBody(BaseModel):
    candidates: list[CandidateIn]


def _clamped_content(content: dict, cap: int) -> dict:
    return {k: (v[:cap] if isinstance(v, str) else v) for k, v in content.items()}


@router.post("/canvas/keep")
async def canvas_keep(project: str, kb: str, body: KeepBody) -> dict:
    """Persist kept candidates through the memory resolver (the HITL gate)."""
    ctx, store = resolve_kb_or_404(project, kb)
    if not body.candidates:
        raise HTTPException(status_code=400, detail="no candidates to keep")

    cfg = get_config()
    ccfg = cfg.canvas
    kept = body.candidates[: ccfg.keep_max_candidates]
    candidates = [
        Finding(
            exploration_id="canvas-keep",
            project_id=ctx.project_id,
            category=c.category,
            title=c.title,
            content=_clamped_content(c.content, ccfg.keep_max_content_chars),
            confidence=c.confidence,
            tags=c.tags,
            provenance=c.provenance,
        )
        for c in kept
    ]

    outcome = await resolve_and_persist(ctx, store, candidates, cfg)
    ids = outcome.affected_finding_ids
    syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
    schedule_kg_update(ctx, ids, store=store)
    return {
        "finding_ids": ids,
        "events": [e.model_dump() for e in outcome.events],
        "synopsis": syn_status,
    }
```

Update the module docstring's keep line if it drifted.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_api_canvas.py -v`
Expected: all PASS.

- [ ] **Step 5: Full suite + lint, commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`

```bash
git add delapan/api/routes_canvas.py tests/test_api_canvas.py
git commit -m "feat(api): POST /canvas/keep — resolver-gated persist returning resolution events"
```

---

### Task 8: Docs sync + final verification

**Files:**
- Modify: `README.md` ("What's inside" table + "Status & roadmap")
- Modify: `docs/plans/2026-07-17-canvas-v1-design.md` (premise-correction note)

**Interfaces:** none — documentation and the repo's docs-with-behavior-change rule.

- [ ] **Step 1: README**

In the "What's inside" table add a row for the canvas surface (match the table's existing voice), e.g.:

```
| `delapan/api/routes_canvas.py` + `delapan/core/canvas/` | `/canvas/search` (SSE: ephemeral web candidates + grounded streamed answer) and `/canvas/keep` (resolver-gated persistence returning ADD/UPDATE/NOOP/SUPERSEDE events) |
```

In "Status & roadmap", mark canvas phase 1 landed and note the two loud-failure fixes (search-provider quota now fails the run; synopsis rebuild routes via gateway and reports `rebuilt/skipped/failed` status).

- [ ] **Step 2: Spec premise note**

In `docs/plans/2026-07-17-canvas-v1-design.md`, "Backend" section, replace the sentence claiming the port basis ("Port, don't reinvent…") with a short correction: the June build is unreachable (remote has no `dev` ref); phase 1 implemented the same contract fresh on master's substrate (`run_exploration` + `resolve_and_persist`).

- [ ] **Step 3: Final verification + commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: full suite green, ruff clean.

```bash
git add README.md docs/plans/2026-07-17-canvas-v1-design.md
git commit -m "docs: canvas phase-1 surface in README; correct spec's port premise"
```

---

## Self-Review Notes (already applied)

- **Spec coverage:** loud-failure explore ⇒ Tasks 1-2; silent synopsis ⇒ Task 3; canvas surface + keep→resolver ⇒ Tasks 4-7; docs rule ⇒ Task 8. Spec items NOT in phase 1 (by design): frontend canvas page, character layer, packaging (phases 2-4).
- **Known deviation from spec text:** spec's keep response sketch was `{finding_ids, events: [{op, finding_id, loser_id?}]}`; this plan ships `ResolutionEvent.model_dump()` verbatim (`new_finding_id`/`target_finding_id`) — richer, and the field mapping is documented in Task 7's Interfaces block. Spec's "port from origin/dev" is corrected in Task 8.
- **Auto-create decision:** canvas routes use `resolve_kb_or_404` like every HTTP route (no KB auto-create over HTTP; MCP keeps `create=True`). June's auto-create-LLM-titled-KB is deferred with the frontend cold-start story (phase 2+).
- **Type consistency check:** `Finding` construction relies on `created_at`/`updated_at` default factories (`models.py:106-107`) — tests that pass them explicitly also fine; `ResolutionDecision(candidate_index=..., op=...)` matches `memory/models.py:23-32`; `select_preamble(query, *, store, kb_id, depth)` matches `preamble.py:131-137`; `text_completion(*, model, system, user, ...)` matches `ai_gateway.py:36-43`.
- **Fixture caveat:** two test files copy the `client`/`kb` fixtures from `test_api_routes.py` rather than sharing via conftest — matches the repo's current no-shared-API-fixture layout; consolidating into `tests/conftest.py` is a reviewer's-choice cleanup, not required.
