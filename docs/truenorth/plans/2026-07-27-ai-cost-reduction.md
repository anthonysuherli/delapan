# AI Cost Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut AI Gateway spend by adding an agent-driven ingest path that persists Claude-Code-extracted findings for embedding cost only, and by trimming the paid pipeline's model and reasoning-effort knobs.

**Architecture:** A new `delapan_add_findings` MCP tool accepts findings the calling agent extracted itself and routes them into `resolve_and_persist` — the existing single finding-persist path — so dedup, tier parity, and grounding all come for free with no new `Store` method. Separately, the shared `exploration.reasoning_effort` knob splits per stage so the planner keeps `high` while extraction drops to `medium`, and `deepen.decompose_model` drops off Opus while its critic stays.

**Vision goals served:** None directly — maintenance work. Implements the Invariant *"AI spend is bounded and observable by construction"* (added 2026-07-27) while preserving the Invariant *"The KG extraction model stays a frontier model by default"*.

**Tech Stack:** Python 3.12+, pydantic v2, FastMCP (`mcp.server.fastmcp`), pytest + pytest-asyncio, ruff (line-length 100), `uv` for running.

## Global Constraints

- Source spec: `docs/truenorth/specs/2026-07-27-ai-cost-reduction-design.md`.
- `from __future__ import annotations` at the top of every touched module; type hints throughout; ruff line-length 100.
- **Never modify `knowledge_graph.extraction_model`** (currently `anthropic/claude-opus-4.8`) or `knowledge_graph.reasoning_effort`. Both are protected by a ratified Invariant.
- All new knobs are config, never hardcoded. Precedence: code defaults < `config.yaml` < env (`DLP_<SECTION>__<FIELD>`).
- Tests must run hermetically against the SQLite tier with no cloud access.
- `provenance` must be non-empty on every persisted finding — the `grounded_in` Invariant.
- Run tests with `cd ~/projects/delapan && uv run pytest`.
- **Baseline:** 2 pre-existing environment-related pytest failures exist on clean `master`. Count them before blaming a change.

---

### Task 1: Split `exploration.reasoning_effort` into per-stage knobs

Pure no-op refactor: both new knobs default to the current value (`high` in `config.yaml`), so behavior is byte-for-byte unchanged. Lowering extraction happens in Task 2, keeping "did the split break anything" separable from "did lowering effort hurt quality".

**Files:**
- Modify: `delapan/core/config.py` (`ExplorationConfig`, around line 276)
- Modify: `delapan/core/exploration/planner.py:81`
- Modify: `delapan/core/exploration/extractor.py:51`
- Test: `tests/test_config_exploration_effort.py` (create)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `ExplorationConfig.planner_reasoning_effort: str | None` and `ExplorationConfig.extraction_reasoning_effort: str | None`. The legacy `ExplorationConfig.reasoning_effort` field is **retained** as the fallback default source for both.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_exploration_effort.py`:

```python
from __future__ import annotations

from delapan.core.config import ExplorationConfig


def test_per_stage_effort_defaults_to_shared_value():
    """Both stage knobs inherit the shared knob when unset — no behavior change."""
    cfg = ExplorationConfig(reasoning_effort="high")
    assert cfg.planner_reasoning_effort == "high"
    assert cfg.extraction_reasoning_effort == "high"


def test_per_stage_effort_overrides_shared_value():
    """An explicit stage value wins over the shared knob."""
    cfg = ExplorationConfig(reasoning_effort="high", extraction_reasoning_effort="medium")
    assert cfg.planner_reasoning_effort == "high"
    assert cfg.extraction_reasoning_effort == "medium"


def test_shared_effort_none_leaves_stages_none():
    """None means provider default; the split must not invent a value."""
    cfg = ExplorationConfig()
    assert cfg.reasoning_effort is None
    assert cfg.planner_reasoning_effort is None
    assert cfg.extraction_reasoning_effort is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_exploration_effort.py -v`
Expected: FAIL — `AttributeError` / pydantic error on `planner_reasoning_effort`.

- [ ] **Step 3: Add the fields and the inheritance validator**

In `delapan/core/config.py`, inside `class ExplorationConfig`, replace the single-line
`reasoning_effort` declaration:

```python
    # Gateway/Gemini thinking level for planning + extraction; None = provider default.
    reasoning_effort: str | None = None  # "low" | "medium" | "high"
```

with:

```python
    # Gateway/Gemini thinking level; None = provider default.
    # `reasoning_effort` is the shared fallback; the per-stage knobs below let the
    # planner stay expensive (it decides query diversity) while the high-volume
    # page extractor runs cheaper. Thinking tokens bill as full-price output.
    reasoning_effort: str | None = None  # "low" | "medium" | "high"
    planner_reasoning_effort: str | None = None
    extraction_reasoning_effort: str | None = None
```

Then add this validator to the same class (place it next to `_check_search_mode`):

```python
    @model_validator(mode="after")
    def _default_stage_effort(self) -> ExplorationConfig:
        # Unset stage knobs inherit the shared value, so introducing the split is
        # a no-op until a stage is explicitly overridden in config.yaml.
        if self.planner_reasoning_effort is None:
            self.planner_reasoning_effort = self.reasoning_effort
        if self.extraction_reasoning_effort is None:
            self.extraction_reasoning_effort = self.reasoning_effort
        return self
```

Confirm `model_validator` is already imported in `config.py` (it is — `DeepenConfig` uses it). If not, add it to the pydantic import line.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_exploration_effort.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Point the two call sites at the new knobs**

In `delapan/core/exploration/planner.py`, line 81, change:

```python
        reasoning_effort=cfg.reasoning_effort,
```

to:

```python
        reasoning_effort=cfg.planner_reasoning_effort,
```

In `delapan/core/exploration/extractor.py`, line 51, change:

```python
            reasoning_effort=cfg.reasoning_effort,
```

to:

```python
            reasoning_effort=cfg.extraction_reasoning_effort,
```

**Do not touch** `delapan/core/exploration/deepen.py:119` — it reads `DeepenConfig.reasoning_effort`, a different config object with its own field.

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `uv run pytest -q`
Expected: same pass/fail counts as the clean-`master` baseline (2 known environment failures, no new ones).

- [ ] **Step 7: Commit**

```bash
git add delapan/core/config.py delapan/core/exploration/planner.py delapan/core/exploration/extractor.py tests/test_config_exploration_effort.py
git commit -m "feat(config): split exploration reasoning_effort into per-stage knobs"
```

---

### Task 2: Apply the cost-reducing config values

Task 1 made the split a no-op. This task is the reviewable one-line-each behavior change.

**Files:**
- Modify: `config.yaml` (`exploration` and `deepen` sections)

**Interfaces:**
- Consumes: `ExplorationConfig.extraction_reasoning_effort` from Task 1.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Lower extraction effort**

In `config.yaml`, in the `exploration:` section, find:

```yaml
  reasoning_effort: high              # Gemini/gateway thinking level for planning + extraction ("low"|"medium"|"high")
```

Replace with:

```yaml
  reasoning_effort: high              # shared fallback; planner inherits it
  # Planning decides query diversity — keep it expensive. There is a known defect
  # where explore collapses onto a single subtopic while coverage reads "rich",
  # and lowering planner effort risks worsening it.
  planner_reasoning_effort: high
  # High-volume, per-page. Thinking tokens bill as full-price output, so this is
  # where the token saving actually comes from.
  extraction_reasoning_effort: medium
```

- [ ] **Step 2: Drop deepen's decompose model off Opus**

In `config.yaml`, in the `deepen:` section, find:

```yaml
  decompose_model: anthropic/claude-opus-4.8
```

Replace with:

```yaml
  # Decomposition is structural fan-out (topic → 3-5 facets) and runs at
  # depth_cap × facets_per_round volume — the cheaper half of deepen.
  decompose_model: google/gemini-3.1-pro-preview
```

**Leave `critic_model: anthropic/claude-opus-4.8` unchanged** — the critic is the judgment gate deciding coverage and when the loop stops.

- [ ] **Step 3: Verify the config parses and the values land**

Run:

```bash
uv run python -c "
from delapan.core.config import get_config
c = get_config()
print('planner  ', c.exploration.planner_reasoning_effort)
print('extract  ', c.exploration.extraction_reasoning_effort)
print('decompose', c.deepen.decompose_model)
print('critic   ', c.deepen.critic_model)
print('KG model ', c.knowledge_graph.extraction_model)
"
```

Expected output:

```
planner   high
extract   medium
decompose google/gemini-3.1-pro-preview
critic    anthropic/claude-opus-4.8
KG model  anthropic/claude-opus-4.8
```

The last line is the Invariant check — if it is anything other than `anthropic/claude-opus-4.8`, stop and revert.

- [ ] **Step 4: Commit**

```bash
git add config.yaml
git commit -m "perf(config): medium extraction effort, gemini decompose; critic and KG unchanged"
```

---

### Task 3: `delapan_add_findings` MCP tool

The cost lever. Accepts findings the calling agent extracted with its own tools and persists them through the existing path. Only `embed_batch` reaches the gateway.

**Files:**
- Modify: `delapan/mcp/server.py` (add `_add_findings_impl` + the `@mcp.tool()` wrapper, after `delapan_explore`)
- Test: `tests/test_mcp_add_findings.py` (create)

**Interfaces:**
- Consumes: `resolve_and_persist(ctx, store, candidates, cfg)` from `delapan.core.memory.persist`; `Finding` from `delapan.core.exploration.models`; `maybe_rebuild_synopsis`, `schedule_kg_update`, `resolve_tenant`, `get_store`, `_clear_archive`, `_now_iso` — all already imported or defined in `server.py`.
- Produces: `_add_findings_impl(ctx: TenantContext, findings: list[dict]) -> dict` and the MCP tool `delapan_add_findings(project, kb, findings)`. Result dict keys: `status`, `count`, `finding_ids`, `synopsis`, `unarchived`, and on rejection `error`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_add_findings.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.memory.models import ResolutionDecision, ResolutionOp
from delapan.mcp import server as server_mod


def _raw(title: str, url: str = "https://example.com/a") -> dict:
    return {
        "title": title,
        "category": "fact",
        "content": {"claim": f"{title} body"},
        "provenance": [{"url": url}],
    }


@pytest.fixture
def ctx_and_store(store, monkeypatch):
    org, pid = store.resolve_project("agentproj", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid, access_token=None)

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    async def _no_synopsis(kb_id, org_id=None, store=None):
        return "skipped"

    monkeypatch.setattr("delapan.core.memory.persist.embed_batch", _fake_embed)
    monkeypatch.setattr("delapan.core.memory.persist.resolve", _all_add)
    monkeypatch.setattr(server_mod, "maybe_rebuild_synopsis", _no_synopsis)
    monkeypatch.setattr(server_mod, "schedule_kg_update", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "get_store", lambda *a, **k: store)
    return ctx, store


@pytest.mark.asyncio
async def test_rejects_finding_without_provenance(ctx_and_store):
    """Grounding is a precondition of the tool, not a caller convention."""
    ctx, store = ctx_and_store
    bad = {"title": "ungrounded", "category": "fact", "content": {"claim": "x"}}
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert "provenance" in out["error"]
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_empty_provenance_list(ctx_and_store):
    ctx, store = ctx_and_store
    bad = _raw("empty prov")
    bad["provenance"] = []
    out = await server_mod._add_findings_impl(ctx, [bad])
    assert "error" in out
    assert store.count_findings(ctx.kb_id) == 0


@pytest.mark.asyncio
async def test_rejects_empty_batch(ctx_and_store):
    ctx, store = ctx_and_store
    out = await server_mod._add_findings_impl(ctx, [])
    assert "error" in out


@pytest.mark.asyncio
async def test_persists_grounded_findings(ctx_and_store):
    """Happy path: findings land and ids come back."""
    ctx, store = ctx_and_store
    out = await server_mod._add_findings_impl(ctx, [_raw("alpha"), _raw("beta")])
    assert out["status"] == "completed"
    assert out["count"] == 2
    assert len(out["finding_ids"]) == 2
    assert store.count_findings(ctx.kb_id) == 2


@pytest.mark.asyncio
async def test_provenance_survives_persistence(ctx_and_store):
    """The grounded_in Invariant: the source url must still be there after the write."""
    ctx, store = ctx_and_store
    url = "https://vercel.com/docs/ai-gateway/pricing"
    await server_mod._add_findings_impl(ctx, [_raw("zero markup", url=url)])

    rows = store.list_findings(ctx.kb_id)
    assert len(rows) == 1
    blob = str(rows[0])
    assert url in blob, "provenance url was dropped between the tool and the store"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_add_findings.py -v`
Expected: FAIL — `AttributeError: module 'delapan.mcp.server' has no attribute '_add_findings_impl'`.

- [ ] **Step 3: Implement `_add_findings_impl`**

In `delapan/mcp/server.py`, add `Finding` to the imports:

```python
from delapan.core.exploration.models import Finding
```

Then add this function immediately after `_explore_impl`:

```python
def _finding_from_raw(ctx: TenantContext, exp_id: str, raw: dict) -> Finding:
    """Build a Finding from an agent-supplied dict. Caller has already validated
    provenance, so this stays a pure shape adapter."""
    return Finding(
        exploration_id=exp_id,
        project_id=ctx.project_id,
        category=raw.get("category") or "fact",
        title=raw["title"],
        content=raw["content"],
        provenance=raw["provenance"],
        confidence=float(raw.get("confidence", 0.5)),
        tags=raw.get("tags") or [],
        entity_type=raw.get("entity_type"),
        extraction_model="agent",
    )


async def _add_findings_impl(ctx: TenantContext, findings: list[dict]) -> dict:
    """Persist agent-extracted findings. No LLM call — the calling agent already
    did the reasoning; only embedding reaches the gateway.

    Every finding must carry non-empty ``provenance``: grounding is a
    precondition here rather than a convention callers must remember."""
    if not findings:
        return {"error": "no findings supplied"}

    for i, raw in enumerate(findings):
        for field in ("title", "content"):
            if not raw.get(field):
                return {"error": f"finding[{i}] is missing required field {field!r}"}
        if not raw.get("provenance"):
            return {
                "error": (
                    f"finding[{i}] has no provenance — every finding must cite at least "
                    "one source url, e.g. provenance=[{'url': 'https://…'}]"
                )
            }

    store = get_store(ctx.access_token, org_id=ctx.org_id)
    was_archived = _clear_archive(store, ctx)

    exp_id = store.create_exploration(ctx.org_id, ctx.kb_id, "agent-ingest")
    try:
        candidates = [_finding_from_raw(ctx, exp_id, raw) for raw in findings]
        outcome = await resolve_and_persist(ctx, store, candidates, get_config())
        ids = outcome.affected_finding_ids
        store.update_exploration(
            exp_id, status="completed", completed_at=_now_iso(), finding_ids=ids
        )
        syn_status = await maybe_rebuild_synopsis(ctx.kb_id, org_id=ctx.org_id, store=store)
        schedule_kg_update(ctx, ids, store=store)
    except Exception as exc:  # noqa: BLE001 — record the failure, then surface it
        store.update_exploration(exp_id, status="failed", completed_at=_now_iso())
        raise exc

    return {
        "exploration_id": exp_id,
        "status": "completed",
        "finding_ids": ids,
        "count": len(ids),
        "synopsis": syn_status,
        "unarchived": was_archived,
    }
```

Note: this deliberately does **not** call `missing_pipeline_keys()`. That gate hard-requires
`OPENAI_API_KEY` and is already known-stale in this OpenAI-free setup; this path needs only
embeddings, which route through the AI Gateway.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_add_findings.py -v`
Expected: PASS (5 passed).

If `test_provenance_survives_persistence` fails because `store.list_findings(kb)` does not exist
with that name, check the read methods on the `Store` protocol in `delapan/store/` and substitute
the correct one — the assertion that the url survives is the load-bearing part, not the accessor.

- [ ] **Step 5: Add the MCP tool wrapper**

In `delapan/mcp/server.py`, immediately after the `delapan_explore` tool definition:

```python
@mcp.tool()
async def delapan_add_findings(project: str, kb: str, findings: list[dict]) -> dict:
    """Persist findings you researched yourself into the named KB (creating the
    project/KB on demand). Use this instead of ``delapan_explore`` when you have
    already searched and read the sources with your own tools — it runs no LLM
    call of its own, so it is far cheaper and returns immediately.

    Each item in ``findings`` requires:
      ``title``       short claim-shaped headline
      ``content``     dict of structured fields (the claim body)
      ``provenance``  non-empty list of ``{"url": ..., "title": ...}`` — the
                      sources you actually read. A finding without provenance is
                      rejected; ungrounded claims never enter the KB.
    Optional: ``category`` (default "fact"), ``confidence`` 0-1, ``tags``,
    ``entity_type``.

    Findings resolve against existing ones (ADD/UPDATE/NOOP/SUPERSEDE), so
    re-submitting overlapping material refines rather than duplicates."""
    try:
        ctx = resolve_tenant(project, kb, create=True)
    except Exception as exc:  # noqa: BLE001 — clean error for a missing project/KB
        return {"error": f"KB not found ({project}/{kb}): {exc}"}
    return await _add_findings_impl(ctx, findings)
```

- [ ] **Step 6: Verify the tool registers**

Run:

```bash
uv run python -c "
import asyncio
from delapan.mcp.server import mcp
names = [t.name for t in asyncio.run(mcp.list_tools())]
print('registered:', 'delapan_add_findings' in names)
print(sorted(names))
"
```

Expected: `registered: True` and `delapan_add_findings` present in the list.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: baseline failures only (2 known environment failures), no new ones.

- [ ] **Step 8: Commit**

```bash
git add delapan/mcp/server.py tests/test_mcp_add_findings.py
git commit -m "feat(mcp): add delapan_add_findings for agent-extracted findings"
```

---

### Task 4: Dedup integration test

Proves the tool inherits resolver behavior rather than appending blindly — the spec's acceptance criterion and a direct echo of the vision's "resolution observably dedupes".

**Files:**
- Modify: `tests/test_mcp_add_findings.py`

**Interfaces:**
- Consumes: `_add_findings_impl` from Task 3.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_add_findings.py`:

```python
@pytest.mark.asyncio
async def test_reingest_updates_instead_of_duplicating(store, monkeypatch):
    """Second submission of the same claim resolves as UPDATE — no duplicate row."""
    org, pid = store.resolve_project("dedupproj", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid, access_token=None)

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    async def _no_synopsis(kb_id, org_id=None, store=None):
        return "skipped"

    monkeypatch.setattr("delapan.core.memory.persist.embed_batch", _fake_embed)
    monkeypatch.setattr(server_mod, "maybe_rebuild_synopsis", _no_synopsis)
    monkeypatch.setattr(server_mod, "schedule_kg_update", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "get_store", lambda *a, **k: store)

    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr("delapan.core.memory.persist.resolve", _all_add)
    monkeypatch.setenv("DLP_MEMORY__ENABLED", "true")
    from delapan.core.config import get_config

    get_config.cache_clear()

    await server_mod._add_findings_impl(ctx, [_raw("gateway is zero markup")])
    assert store.count_findings(kb) == 1
    existing = store.list_findings(kb)[0]

    # Second pass — resolver says UPDATE against the existing row.
    async def _all_update(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(
                candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=existing["id"]
            )
        ]

    monkeypatch.setattr("delapan.core.memory.persist.resolve", _all_update)
    await server_mod._add_findings_impl(ctx, [_raw("gateway is zero markup")])

    get_config.cache_clear()
    assert store.count_findings(kb) == 1, "UPDATE must not create a second row"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_mcp_add_findings.py::test_reingest_updates_instead_of_duplicating -v`
Expected: PASS. If it fails on `store.list_findings` signature, check the `Store` protocol in `delapan/store/` and use the correct read method — the assertion on `count_findings` is the load-bearing one.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_add_findings.py
git commit -m "test(mcp): add_findings resolves duplicates instead of appending"
```

---

### Task 5: Agent-driven research skill

Gives the tool a front door so it is actually used instead of `delapan_explore`.

**Files:**
- Create: `skills/ingest/SKILL.md`

**Interfaces:**
- Consumes: the `delapan_add_findings` MCP tool from Task 3.
- Produces: nothing.

- [ ] **Step 1: Read the existing skill for house style**

Run: `cat skills/explore/SKILL.md`
Match its frontmatter shape (`name`, `description`) and section conventions.

- [ ] **Step 2: Write the skill**

Create `skills/ingest/SKILL.md`:

```markdown
---
name: ingest
description: Research a topic using your own web search and reading, then persist the findings to the KB via delapan_add_findings. Use INSTEAD of /delapan:explore when you can search the web yourself — it costs no pipeline LLM spend. Falls back to /delapan:explore for unattended or deployed runs.
---

# Delapan Ingest (agent-driven)

Research with your own tools; delapan only stores and dedupes. No pipeline LLM
call is made, so this is roughly 1/1000th the gateway cost of `delapan_explore`.

## When to use

Use this when you (the agent) have web search and fetch available. Use
`/delapan:explore` instead when the research must run unattended, on a schedule,
or on a deployed surface with no agent in the loop.

## Target resolution

    git rev-parse --show-toplevel | xargs basename   # → project
    git branch --show-current                         # → kb

## Workflow

1. **Decompose** the topic into 3-5 genuinely distinct sub-questions. Do this
   explicitly — the paid pipeline has a known defect where it collapses onto one
   subtopic, and you avoid it only by deliberately covering different ground.
2. **Search and read** for each sub-question with your own tools. Prefer primary
   sources; read the page rather than trusting the search snippet.
3. **Extract findings.** One claim per finding. A finding is a specific, checkable
   statement — not a summary of a page. Put the claim in `title` and the
   supporting structure in `content`.
4. **Cite everything.** Every finding needs `provenance` with the URL you actually
   read. Findings without provenance are rejected.
5. **Persist** with `delapan_add_findings(project, kb, findings)`.
6. **Confirm** with `delapan_resume` to see refreshed coverage.

## Finding shape

    {
      "title": "Vercel AI Gateway charges zero markup over provider list price",
      "category": "fact",
      "content": {"markup": "0%", "applies_to": "including BYOK"},
      "provenance": [{"url": "https://vercel.com/docs/ai-gateway/pricing"}],
      "confidence": 0.8,
      "tags": ["pricing"]
    }

## Quality bar

- Set `confidence` honestly: ~0.9 for a primary-source number, ~0.5 for a single
  secondary blog, lower for anything inferred.
- Do not submit a finding you could not point at a specific line of a source for.
- Re-submitting overlapping material is safe — the resolver refines rather than
  duplicates.
```

- [ ] **Step 3: Verify the skill file parses**

Run: `head -5 skills/ingest/SKILL.md`
Expected: valid YAML frontmatter with `name: ingest`.

- [ ] **Step 4: Commit**

```bash
git add skills/ingest/SKILL.md
git commit -m "feat(skills): add agent-driven ingest skill"
```

---

### Task 6: Gateway budget keys — MANUAL RUNBOOK, requires explicit user approval

> **⚠️ Do NOT execute this task autonomously.** It creates billable configuration
> on the user's Vercel account. The user must approve and is expected to run these
> commands themselves. Its purpose in this plan is to be a checklist, not an action.

**Files:**
- Modify: `.env` (local, uncommitted) — swap in the per-pipeline key
- Modify: `docs/truenorth/specs/2026-07-27-ai-cost-reduction-design.md` — record actual key names once created

**Interfaces:**
- Consumes: nothing. Produces: nothing consumed by code.

- [ ] **Step 1: Confirm auto top-up is OFF**

Vercel dashboard → AI → Credits. Auto top-up must be **off** so the prepaid balance
stays a natural hard ceiling. A $0 balance returns HTTP 402 on every engine call.

- [ ] **Step 2: Create the three budgeted keys**

```bash
npx vercel ai-gateway api-keys create --budget 30 --refresh-period monthly
npx vercel ai-gateway api-keys create --budget 10 --refresh-period monthly
npx vercel ai-gateway api-keys create --budget 10 --refresh-period monthly
```

Label them `explore`, `kg`, and `agent` respectively. Budgets are enforced
**before every request** — this is the only real-time kill switch, and the
mechanism that would have contained the previously observed wedged explore run.

- [ ] **Step 3: Verify a budget actually hard-stops**

Create a scratch key with a $0.01 budget, point `AI_GATEWAY_API_KEY` at it, and run
any explore. Expected: the request is refused pre-flight with an insufficient-budget
error, not a partial run. Delete the scratch key afterward.

- [ ] **Step 4: Point the engine at the explore key**

Set `AI_GATEWAY_API_KEY` in `~/projects/delapan/.env` to the `explore` key.
Do **not** commit `.env`.

- [ ] **Step 5: Vercel dashboard sweep**

- Builds → pin to **Standard** machines (Elastic is default-on for new Pro teams and bills $0.0035/CPU-min).
- Enable the free **WAF Bot Protection** ruleset; enable Deployment Protection on preview URLs.
- Delete the stray `clean-wt` project.
- Set Spend Management alerts at 50/75/100% but leave **auto-pause OFF** — it halts production across every team project with a `503 DEPLOYMENT_PAUSED` and never unpauses on its own.

---

## Dependency note

The "observable" half of the spend Invariant — metering every LLM call regardless of entry path —
is **not** in this plan. It is being implemented separately (moving instrumentation down to
`delapan/core/clients/ai_gateway.py`). Until that lands, `usage_events` will not record the
savings this plan produces, so verify cost changes against the Vercel AI Gateway observability
dashboard rather than `/ops/costs`.

## Known drift found while planning

`ExplorationConfig.search_mode` (`auto` | `agent` | `tavily`) is **dead config** — only its
validator references it; no code reads the value. The related `ingest_pages()` in
`core/exploration/engine.py` documents itself as shared with "the agent-handoff path", which was
scaffolded but never wired. That path is also weaker than this plan's: it still LLM-extracts, so it
would save Tavily cost only. Worth either wiring or deleting in separate work — not touched here.
