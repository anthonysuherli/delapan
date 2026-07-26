# Claude Code Plugin Release Interface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the delapan Claude Code plugin installable by a stranger — portable shell in this repo, uv-run bootstrap, seeded demo KB, onboarding card, actionable key errors, and no silent-empty explores.

**Architecture:** The plugin shell (`.claude-plugin/`, `skills/`, `mcp.json`, wrapper script) lands at this repo's root so engine and plugin version together. A new `delapan/mcp/onboarding.py` module owns first-run affordances (demo seed, KB-not-found card); the existing MCP tool layer in `delapan/mcp/server.py` gains key preflights and an explicit `empty` explore status, all inherited by `cloud_server.py` through the shared `_*_impl` functions.

**Vision goals served:** the engine "tapped by Claude Code" (core framing); the open-core local tier as the public free story; on-ramp to End Goal "Hosted public tier with account isolation".

**Tech Stack:** Python ≥3.11, uv (`uv.lock` committed), FastMCP (`mcp>=1.16.0`), SQLite + sqlite-vec (local tier), pytest + pytest-asyncio, bash wrapper.

**Spec:** `docs/truenorth/specs/2026-07-26-plugin-release-interface-design.md` (amended by Task 0).

## Global Constraints

- ruff line-length 100; `from __future__ import annotations`; full type hints; terse module docstrings with ASCII flow diagram (house style).
- Offline test suite stays hermetic: SQLite tier only, no network, no cloud project. Baseline: **2 pre-existing env-related pytest failures on clean master** — do not count them as regressions, do not fix them here.
- No `Store` protocol changes anywhere in this plan.
- Local tier stays auth-less; no new hardcoded knobs (bootstrap paths are constants, not tunables).
- License is `AGPL-3.0-or-later` — every shipped manifest must say so.
- Work on a dedicated branch `feat/plugin-release` cut from `master` (use truenorth:using-git-worktrees). Conventional-commit prefixes (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- Tool-layer results are plain dicts; error results always carry an `"error"` key so existing clients degrade unchanged.
- Repo facts this plan relies on (verified 2026-07-26): default DB `~/.delapan/delapan.db` via `_default_db_path()` (`delapan/store/sqlite.py:201`); `.env` and `config.yaml` resolve module-relative to the repo root; all `Settings` fields optional; `select_preamble(query=None)` performs no embedding; `.gitignore` ignores `*.db`; `sqlite-vec` lives in the `[local]` extra (wrapper must pass `--extra local`).

---

### Task 0: Amend the spec to match verified code reality

**Files:**
- Modify: `docs/truenorth/specs/2026-07-26-plugin-release-interface-design.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the corrected acceptance criteria the rest of the plan implements. Later tasks cite: zero-key demo = `resume` without query; `api/deps.py` gate = verify-only.

Two spec claims are wrong against the code: (a) `delapan_search` always embeds the query (`_search_impl` → `embed_text`), so search can never be zero-key — the zero-key demo surface is `delapan_resume` with no query (synopsis render, no embedding) plus `delapan_projects`; (b) the "stale `OPENAI_API_KEY` gate" in `api/deps.py` is already fixed — `missing_pipeline_keys()` requires only `TAVILY_API_KEY` + `AI_GATEWAY_API_KEY`.

- [ ] **Step 1: Fix the demo bullet in §2 First-run UX**

Replace:

```
  First `delapan_projects` already lists the demo project; `delapan_search` returns
  grounded findings with zero keys.
```

With:

```
  First `delapan_projects` already lists the demo project, and `delapan_resume`
  (no query) renders the demo synopsis with zero keys. `delapan_search` embeds
  the query, so it activates once the one key is set.
```

- [ ] **Step 2: Fix acceptance criterion 2**

Replace:

```
2. `/delapan:search` on the demo KB returns grounded findings with zero keys.
```

With:

```
2. `/delapan:resume` on the demo KB renders its synopsis with zero keys; with
   the single key set, `/delapan:search` returns grounded findings.
```

- [ ] **Step 3: Fix the second landmine bullet**

Replace:

```
  - The stale `OPENAI_API_KEY` gate in `api/deps.py` is removed/corrected to
    match the gateway-based credential model.
```

With:

```
  - (Verified already fixed in code) `api/deps.py`'s `missing_pipeline_keys()`
    treats the gateway key as the only required provider credential. This
    release moves it to `delapan/core/config.py`, reuses it as the MCP explore
    preflight, and pins it with a regression test.
```

- [ ] **Step 4: Commit**

```bash
git add docs/truenorth/specs/2026-07-26-plugin-release-interface-design.md
git commit -m "docs: amend plugin-release spec to verified code reality (zero-key=resume; deps gate already fixed)"
```

---

### Task 1: Plugin shell — wrapper, mcp.json, manifests, .env.example

**Files:**
- Create: `scripts/mcp-server.sh`, `mcp.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.env.example`

**Interfaces:**
- Consumes: `python -m delapan.mcp.server` (exists), `[project.optional-dependencies] local` extra in `pyproject.toml`.
- Produces: `${CLAUDE_PLUGIN_ROOT}/scripts/mcp-server.sh` as the only MCP launch path; `.claude-plugin/plugin.json` naming six skills at `./skills/<name>` (created in Task 2 — plugin.json may reference them now; nothing validates until install).

- [ ] **Step 1: Write the wrapper** — `scripts/mcp-server.sh`:

```bash
#!/usr/bin/env bash
# delapan MCP server launcher. Portable: uv materializes the env from uv.lock
# on first run; no venv paths are baked in anywhere.
set -euo pipefail

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if ! command -v uv >/dev/null 2>&1; then
  echo "delapan: 'uv' is required but not installed — get it with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

cd "$ROOT"
exec uv run --project "$ROOT" --extra local python -m delapan.mcp.server
```

Then: `chmod +x scripts/mcp-server.sh`

- [ ] **Step 2: Write `mcp.json`** (repo root):

```json
{
  "mcpServers": {
    "delapan": {
      "type": "stdio",
      "command": "${CLAUDE_PLUGIN_ROOT}/scripts/mcp-server.sh"
    }
  }
}
```

- [ ] **Step 3: Write `.claude-plugin/plugin.json`**:

```json
{
  "name": "delapan",
  "description": "Agentic knowledge-base engine — ground answers in maintained findings, fill gaps from the web. Skills: /delapan:resume (tap KB → preamble + coverage resume card), /delapan:search (semantic recall over findings), /delapan:explore (gap-fill from the web), /delapan:backlog (curation backlog — gaps ranked by demand), /delapan:projects (cross-repo discovery), /delapan:model (switch engine LLM lineup by optimization profile). Backed by the delapan MCP server.",
  "version": "0.2.0",
  "author": {
    "name": "Anthony Suherli",
    "email": "anthonysuherli@gmail.com"
  },
  "homepage": "https://delapan.ai",
  "license": "AGPL-3.0-or-later",
  "keywords": ["delapan", "knowledge-base", "grounding", "findings", "preamble", "explore", "mcp"],
  "skills": [
    "./skills/resume",
    "./skills/search",
    "./skills/explore",
    "./skills/backlog",
    "./skills/projects",
    "./skills/model"
  ],
  "mcpServers": "./mcp.json"
}
```

- [ ] **Step 4: Write `.claude-plugin/marketplace.json`**:

```json
{
  "name": "delapan",
  "owner": {
    "name": "Anthony Suherli",
    "email": "anthonysuherli@gmail.com"
  },
  "metadata": {
    "description": "delapan — grounding engine plugin: ground → grow → answer, on the delapan MCP server.",
    "version": "0.2.0"
  },
  "plugins": [
    {
      "name": "delapan",
      "source": "./",
      "description": "Agentic knowledge-base engine — ground answers in maintained findings, fill gaps from the web. /delapan:resume, /delapan:search, /delapan:explore, /delapan:backlog, /delapan:projects, /delapan:model over the delapan MCP server.",
      "version": "0.2.0",
      "strict": false
    }
  ]
}
```

- [ ] **Step 5: Write `.env.example`**:

```bash
# delapan credentials — copy to `.env` in this directory (the plugin root).
# One key powers the whole pipeline (LLM calls + embeddings) via Vercel AI Gateway:
AI_GATEWAY_API_KEY=
# Web search for /delapan:explore (optional — resume works without it):
TAVILY_API_KEY=
# Alternative: direct OpenAI for embeddings/extraction instead of the gateway.
# OPENAI_API_KEY=
```

- [ ] **Step 6: Verify the wrapper boots the server**

```bash
bash -n scripts/mcp-server.sh   # syntax check — expect no output
CLAUDE_PLUGIN_ROOT="$PWD" scripts/mcp-server.sh < /dev/null && echo WRAPPER-OK
```

Expected: `WRAPPER-OK` — the stdio server starts, reads EOF from `/dev/null`, and exits cleanly (no `timeout` needed; that command doesn't exist on stock macOS). A `uv is required` message means uv is missing on this machine.

- [ ] **Step 7: Commit**

```bash
git add scripts/mcp-server.sh mcp.json .claude-plugin/ .env.example
git commit -m "feat: portable Claude Code plugin shell (uv-run wrapper, manifests, .env.example)"
```

---

### Task 2: Move + sanitize the six skills

**Files:**
- Create: `skills/resume/SKILL.md`, `skills/search/SKILL.md`, `skills/explore/SKILL.md`, `skills/backlog/SKILL.md`, `skills/projects/SKILL.md`, `skills/model/SKILL.md` (copied from `~/Repositories/8star/delapan-ai/skills/`)

**Interfaces:**
- Consumes: the old shell's skill files; Task 3's result contract (`onboarding` / `try_demo` keys on the resume card — referenced in prose only).
- Produces: the shipped `skills/` tree that `plugin.json` (Task 1) points at.

- [ ] **Step 1: Copy the six skills (not `tracking`)**

```bash
mkdir -p skills
for s in resume search explore backlog projects model; do
  cp -R ~/Repositories/8star/delapan-ai/skills/$s skills/
done
```

- [ ] **Step 2: Sanitize path references**

In `skills/explore/SKILL.md`: frontmatter description — replace `Requires LLM and Tavily keys in backend/.env.` with `Requires AI_GATEWAY_API_KEY and TAVILY_API_KEY in the plugin's .env.`; in the body, replace the line listing `ANTHROPIC_API_KEY, AI_GATEWAY_API_KEY, or OPENAI_API_KEY in backend/.env` with:

```markdown
- `AI_GATEWAY_API_KEY` (and `TAVILY_API_KEY`) in the plugin root's `.env` — copy `.env.example`
```

In `skills/model/SKILL.md`: replace `backend/config.yaml` with `config.yaml` (plugin root) and `backend/.env` with `.env`.

- [ ] **Step 3: Add failure-mode guidance**

Append to `skills/resume/SKILL.md` (after the "When to use" section):

```markdown
## Failure modes

- Result contains `onboarding`: no KB exists for this repo yet. Surface the
  guidance verbatim; offer `/delapan:explore` to seed it. If the result also
  contains `try_demo`, offer the bundled demo KB (`project=delapan`, `kb=demo`).
- Result contains `error` mentioning a credential: tell the user to copy
  `.env.example` to `.env` in the plugin root and set the named variable.
```

Append to `skills/search/SKILL.md` and `skills/explore/SKILL.md`:

```markdown
## Failure modes

- Result contains `error` mentioning a credential: tell the user to copy
  `.env.example` to `.env` in the plugin root and set the named variable.
- Explore result has `status: "empty"`: no findings were produced — relay the
  `reason` field verbatim (never report an empty run as success).
```

- [ ] **Step 4: Verify no personal references remain**

```bash
grep -rn "anthonysuherli\|8star\|backend/\|Repositories" skills/ && echo FAIL || echo CLEAN
```

Expected: `CLEAN`.

- [ ] **Step 5: Commit**

```bash
git add skills/
git commit -m "feat: ship six sanitized plugin skills (resume, search, explore, backlog, projects, model)"
```

---

### Task 3: Onboarding card on KB-not-found (TDD)

**Files:**
- Create: `delapan/mcp/onboarding.py`, `tests/test_onboarding.py`
- Modify: `delapan/mcp/server.py:82-86` (resume except-branch), `delapan/mcp/cloud_server.py:72-75` (same pattern)

**Interfaces:**
- Consumes: `Store.list_projects(include_archived=...)` (rows carry `"project"` name key); `resolve_store()` from `delapan/mcp/tenancy.py`.
- Produces: `delapan.mcp.onboarding.DEMO_PROJECT = "delapan"`, `DEMO_KB = "demo"`, `BUNDLED_DEMO_DB: Path`, `kb_not_found_card(project: str, kb: str, exc: Exception, store: Store | None = None) -> dict` (keys: `error`, `coverage="gap"`, `onboarding`, optional `try_demo`), and `seed_demo_if_absent() -> None` (body added in Task 6 consumes `BUNDLED_DEMO_DB`; declare only the card + constants here).

- [ ] **Step 1: Write the failing tests** — `tests/test_onboarding.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra local pytest tests/test_onboarding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.mcp.onboarding'`

- [ ] **Step 3: Implement** — `delapan/mcp/onboarding.py`:

```python
"""First-run affordances for the MCP surface.

    KB not found ──► kb_not_found_card() ──► gap verdict + guidance (+ demo offer)
    server start ──► seed_demo_if_absent() ──► copy bundled data/demo.db (Task 6)

Shared by the local stdio server and the cloud connector: the demo offer
self-suppresses wherever no demo project exists (cloud never seeds one).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from delapan.store import Store

DEMO_PROJECT = "delapan"
DEMO_KB = "demo"

# onboarding.py lives at <root>/delapan/mcp/onboarding.py → <root>/data/demo.db
BUNDLED_DEMO_DB = Path(__file__).resolve().parents[2] / "data" / "demo.db"


def _demo_available(store: Store | None) -> bool:
    if store is None:
        return False
    try:
        projects = store.list_projects(include_archived=True)
    except Exception:  # noqa: BLE001 — the card must never raise
        return False
    return any(p.get("project") == DEMO_PROJECT for p in projects)


def kb_not_found_card(
    project: str, kb: str, exc: Exception, store: Store | None = None
) -> dict:
    """KB-not-found result with onboarding guidance instead of a bare error."""
    card = {
        "error": f"KB not found ({project}/{kb}): {exc}",
        "coverage": "gap",
        "onboarding": (
            f"No KB exists yet for {project}/{kb}. Run /delapan:explore (the "
            "delapan_explore tool) with a prompt to research and seed it — needs "
            "AI_GATEWAY_API_KEY and TAVILY_API_KEY in the plugin root's .env."
        ),
    }
    if _demo_available(store):
        card["try_demo"] = (
            f'delapan_resume(project="{DEMO_PROJECT}", kb="{DEMO_KB}") — bundled demo '
            "KB about delapan itself; its resume card works with no keys configured."
        )
    return card
```

- [ ] **Step 4: Wire the local server** — in `delapan/mcp/server.py`, add to the imports from `.tenancy`/siblings:

```python
from .onboarding import kb_not_found_card
```

and replace the `delapan_resume` except-branch (lines 83-86):

```python
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — onboarding card for a missing project/KB
        return kb_not_found_card(project, kb, exc, store=resolve_store())
```

- [ ] **Step 5: Wire the cloud server** — in `delapan/mcp/cloud_server.py`, import `kb_not_found_card` from `.onboarding` and replace only the `delapan_resume` except-branch:

```python
    except Exception as exc:  # noqa: BLE001 — onboarding card for a missing project/KB
        return kb_not_found_card(
            project, kb, exc, store=resolve_store_for_token(caller.subject, caller.token)
        )
```

(`resolve_store_for_token` is already imported there; if not, add it from `.tenancy`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --extra local pytest tests/test_onboarding.py tests/test_mcp_smoke.py tests/test_cloud_server.py -v`
Expected: PASS (smoke + cloud suites confirm no contract regressions; only the KB-not-found shape gained keys).

- [ ] **Step 7: Commit**

```bash
git add delapan/mcp/onboarding.py delapan/mcp/server.py delapan/mcp/cloud_server.py tests/test_onboarding.py
git commit -m "feat: onboarding card on KB-not-found (demo offer self-suppresses without demo project)"
```

---

### Task 4: Actionable credential errors (TDD)

**Files:**
- Create: `tests/test_key_errors.py`
- Modify: `delapan/core/clients/embeddings.py:16-29` (`_get_client`), `delapan/core/config.py` (add `missing_pipeline_keys`), `delapan/api/deps.py:33-45` (re-export), `delapan/mcp/server.py` (`delapan_resume`, `delapan_search`, `_explore_impl` preflight)

**Interfaces:**
- Consumes: `Settings` fields `ai_gateway_api_key` / `openai_api_key` / `tavily_api_key`.
- Produces: `delapan.core.clients.embeddings.MissingEmbeddingKeyError(RuntimeError)`; `delapan.core.config.missing_pipeline_keys() -> list[str]` (canonical home; `delapan.api.deps` re-exports it). Explore error contract: `{"error": "explore needs credentials: ..."}` returned before any exploration row is created.

- [ ] **Step 1: Write the failing tests** — `tests/test_key_errors.py`:

```python
"""Missing credentials must produce one actionable message, never a traceback."""

from __future__ import annotations

import pytest


@pytest.fixture()
def keyless_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "keys.db"))
    for key in ("OPENAI_API_KEY", "TAVILY_API_KEY", "AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "")  # force-empty: the repo .env sits below process env
    from delapan.core import config as cfg
    from delapan.core.clients import embeddings as emb

    cfg.get_settings.cache_clear()
    monkeypatch.setattr(emb, "_client", None)
    yield
    cfg.get_settings.cache_clear()


def test_missing_pipeline_keys_lists_both(keyless_env):
    from delapan.core.config import missing_pipeline_keys

    assert missing_pipeline_keys() == ["TAVILY_API_KEY", "AI_GATEWAY_API_KEY"]


def test_deps_reexport_still_works(keyless_env):
    from delapan.api.deps import missing_pipeline_keys

    assert "AI_GATEWAY_API_KEY" in missing_pipeline_keys()


def test_embed_client_raises_actionable(keyless_env):
    from delapan.core.clients.embeddings import MissingEmbeddingKeyError, _get_client

    with pytest.raises(MissingEmbeddingKeyError, match="AI_GATEWAY_API_KEY"):
        _get_client()


@pytest.mark.asyncio
async def test_search_returns_error_dict(keyless_env):
    import delapan.mcp.server as s
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("p", create=True)
    store.resolve_kb(org_id, project_id, "k", create=True)
    res = await s.delapan_search("p", "k", "anything")
    assert "AI_GATEWAY_API_KEY" in res["error"] and ".env" in res["error"]


@pytest.mark.asyncio
async def test_explore_preflight_blocks_before_run(keyless_env):
    import delapan.mcp.server as s

    res = await s.delapan_explore("p2", "k2", prompt="topic")
    assert "explore needs credentials" in res["error"]
    assert "TAVILY_API_KEY" in res["error"] and "AI_GATEWAY_API_KEY" in res["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra local pytest tests/test_key_errors.py -v`
Expected: FAIL — `ImportError` on `missing_pipeline_keys` from `delapan.core.config` and on `MissingEmbeddingKeyError`.

- [ ] **Step 3: Move `missing_pipeline_keys` to `delapan/core/config.py`** (append after `get_settings`):

```python
def missing_pipeline_keys() -> list[str]:
    """Credentials the research pipeline needs end-to-end (search + LLM + embeddings).

    The AI Gateway carries both the LLM calls and the embeddings, so it is the
    only provider credential required; OPENAI_API_KEY is just the embeddings
    fallback when no gateway key is set, never a requirement.
    """
    s = get_settings()
    required = (
        ("TAVILY_API_KEY", s.tavily_api_key),
        ("AI_GATEWAY_API_KEY", s.ai_gateway_api_key),
    )
    return [name for name, value in required if not value]
```

In `delapan/api/deps.py`: delete the local `missing_pipeline_keys` definition (lines 33-45) and add to its imports:

```python
from delapan.core.config import get_settings, missing_pipeline_keys  # noqa: F401 — re-export
```

(drop the now-unused direct `get_settings` import if nothing else in the file uses it).

- [ ] **Step 4: Actionable embeddings error** — in `delapan/core/clients/embeddings.py`, add after the imports:

```python
class MissingEmbeddingKeyError(RuntimeError):
    """No embedding credential configured — actionable, never a traceback."""
```

and change the `else` branch of `_get_client` to:

```python
        elif settings.openai_api_key:
            _client = AsyncOpenAI(api_key=settings.openai_api_key)
        else:
            raise MissingEmbeddingKeyError(
                "no embedding credential configured — set AI_GATEWAY_API_KEY "
                "(or OPENAI_API_KEY) in the plugin root's .env (see .env.example)"
            )
```

- [ ] **Step 5: Wire the MCP tools** — in `delapan/mcp/server.py`:

Add imports:

```python
from delapan.core.clients.embeddings import MissingEmbeddingKeyError, embed_text
from delapan.core.config import get_config, get_settings, missing_pipeline_keys
```

Wrap the impl calls in `delapan_resume` and `delapan_search`:

```python
    try:
        return await _resume_impl(ctx, query, depth)
    except MissingEmbeddingKeyError as exc:
        return {"error": str(exc)}
```

```python
    try:
        return await _search_impl(ctx, query, limit)
    except MissingEmbeddingKeyError as exc:
        return {"error": str(exc)}
```

Add the preflight as the first lines of `_explore_impl` (before the promptless-backlog block, so no row or topic is consumed):

```python
    missing = missing_pipeline_keys()
    if missing:
        return {
            "error": "explore needs credentials: set "
            + " and ".join(missing)
            + " in the plugin root's .env (see .env.example) — resume works without them."
        }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --extra local pytest tests/test_key_errors.py tests/test_api_routes.py tests/test_mcp_smoke.py -v`
Expected: PASS. If any existing test that drives explore now hits the preflight, find it with `grep -rln "delapan_explore\|_explore_impl" tests/` and add to its fixture:

```python
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    cfg.get_settings.cache_clear()
```

- [ ] **Step 7: Commit**

```bash
git add delapan/core/config.py delapan/api/deps.py delapan/core/clients/embeddings.py delapan/mcp/server.py tests/test_key_errors.py
git commit -m "feat: actionable credential errors — embeddings key error, explore preflight, missing_pipeline_keys moved to core"
```

---

### Task 5: Explore `empty` status — no silent zero-finding success (TDD)

**Files:**
- Create: `tests/test_explore_empty.py`
- Modify: `delapan/mcp/server.py` (`_explore_impl` lines 189-226, docstring of `delapan_explore`)

**Interfaces:**
- Consumes: `run_exploration(...) -> list[Finding]` (returns `[]` when search yields nothing); `store.update_exploration(exploration_id, **patch)`; Task 4's preflight (tests set fake keys to get past it).
- Produces: explore result contract — success carries `"status": "completed"`; a zero-finding run carries `"status": "empty"` + `"reason"` and its exploration row is marked `status="empty"`; an all-duplicate run carries `"note"`. Consumed by the explore skill (Task 2) and the spec's acceptance criterion 3.

- [ ] **Step 1: Write the failing tests** — `tests/test_explore_empty.py`:

```python
"""A zero-finding explore must say so — never a silent 'completed' with count 0."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "empty.db"))
    # fake keys: get past the Task-4 preflight without any network use
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()
    yield tmp_path / "empty.db"
    cfg.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_empty_run_reports_empty_status(env, monkeypatch):
    import delapan.mcp.server as s

    async def _no_findings(prompt, **kwargs):
        return []

    monkeypatch.setattr(s, "run_exploration", _no_findings)
    res = await s.delapan_explore("proj", "kb", prompt="anything")

    assert res["status"] == "empty"
    assert res["count"] == 0
    assert "reason" in res and "Tavily" in res["reason"]

    row = sqlite3.connect(env).execute(
        "SELECT status FROM explorations WHERE id = ?", (res["exploration_id"],)
    ).fetchone()
    assert row[0] == "empty"


@pytest.mark.asyncio
async def test_empty_promptless_run_returns_topic_to_backlog(env, monkeypatch):
    import delapan.mcp.server as s
    from delapan.core.config import CurationConfig
    from delapan.core.curation.recorder import _record
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("proj2", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "kb2", create=True)
    await _record(  # seed one gap topic — same idiom as test_curation_flywheel_e2e.py
        store,
        kb_id=kb_id,
        org_id=org_id,
        surface="resume",
        query="an unanswered demo question",
        coverage="gap",
        bands={1: [], 2: [], 3: []},
        embedding=[0.01] * 1536,
        cfg=CurationConfig(prune_sample_rate=0.0),
    )

    async def _no_findings(prompt, **kwargs):
        return []

    monkeypatch.setattr(s, "run_exploration", _no_findings)
    res = await s.delapan_explore("proj2", "kb2", prompt=None)
    assert res["status"] == "empty"

    # consumed topics leave the default backlog view; the empty run must have
    # un-consumed the topic, so it is visible again
    topics = await store.list_curation_topics(kb_id)
    assert topics, "empty run must return the consumed topic to the backlog"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra local pytest tests/test_explore_empty.py -v`
Expected: FAIL — `res["status"]` raises `KeyError` (today's result has no `status` key) or asserts `"completed"`.

- [ ] **Step 3: Implement** — in `_explore_impl` (`delapan/mcp/server.py`), right after `captured = findings[:cap]`:

```python
        if not captured:
            store.update_exploration(
                exp_id, status="empty", completed_at=_now_iso(), finding_ids=[]
            )
            if topic_id:  # the gap was not filled — return the topic to the backlog
                await store.update_curation_topic(ctx.kb_id, topic_id, consumed_at=None)
            return {
                "exploration_id": exp_id,
                "status": "empty",
                "count": 0,
                "reason": (
                    "the pipeline produced no findings — search returned nothing "
                    "(check your Tavily quota) or every extracted finding fell below "
                    "the confidence floor; retry with a more specific prompt"
                ),
                "unarchived": was_archived,
            }
```

and extend the success dict:

```python
    out = {
        "exploration_id": exp_id,
        "status": "completed",
        "finding_ids": ids,
        "count": len(ids),
        "synopsis": syn_status,
        "unarchived": was_archived,
    }
    if not ids:
        out["note"] = "all findings resolved as duplicates of existing knowledge (no new rows)"
```

Update the `delapan_explore` docstring's return description: add `"status"` (``"completed"`` or ``"empty"`` — an empty run adds ``"reason"`` and returns any consumed backlog topic).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra local pytest tests/test_explore_empty.py tests/test_explore_loud_failure.py tests/test_curation_flywheel_e2e.py -v`
Expected: PASS (loud-failure and flywheel suites pin that failed runs and non-empty runs are unchanged).

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/server.py tests/test_explore_empty.py
git commit -m "fix: explore reports status=empty with reason instead of silent completed/count=0"
```

---

### Task 6: Demo KB — builder script + committed artifact (with tests)

**Files:**
- Create: `scripts/demo_findings.yaml`, `scripts/build_demo_db.py`, `data/demo.db` (generated), `tests/test_demo_db.py`
- Modify: `.gitignore` (negate `*.db` for the artifact)

**Interfaces:**
- Consumes: `SQLiteStore(path)`, `store.insert_findings(rows)` (row shape as in `tests/test_mcp_smoke.py`), `store.upsert_synopsis(kb_id, content, finding_count, model)` with `content: list[{"topic", "gloss"}]`, `embed_batch(texts)` (maintainer-run; needs a key at build time only).
- Produces: `data/demo.db` containing project `delapan` / kb `demo` with ≥8 embedded findings and a synopsis — consumed by Task 7's seeder and Task 9's e2e.

- [ ] **Step 1: Write the curated content** — `scripts/demo_findings.yaml`:

```yaml
# Source of truth for data/demo.db — rebuild with scripts/build_demo_db.py.
synopsis:
  - topic: What delapan is
    gloss: an agentic knowledge-base engine — research/ingest the web into per-project KBs, tap them as grounded context
  - topic: Findings
    gloss: the embedded unit of knowledge; every finding keeps grounded_in provenance
  - topic: The resume loop
    gloss: delapan_resume injects a preamble (synopsis + query-relevant findings) with a rich/sparse/gap coverage verdict
  - topic: Explore pipeline
    gloss: plan → search → crawl → extract → evaluate → merge; persists deduplicated findings
  - topic: Self-correcting memory
    gloss: writes resolve ADD/UPDATE/NOOP/SUPERSEDE against existing knowledge — nothing is deleted, only retired
  - topic: Two storage tiers
    gloss: local SQLite + sqlite-vec (this install) and Supabase cloud, at strict Store-protocol parity

findings:
  - title: delapan grounds answers in maintained findings
    summary: delapan is an agentic knowledge-base engine. It researches and ingests
      the web into per-project knowledge bases of embedded, deduplicated findings,
      then serves them back as grounded context through MCP tools and skills.
    category: architecture
    tags: [overview]
  - title: A finding is the unit of knowledge
    summary: Each finding is an embedded row with a title, summary content, category,
      confidence, and grounded_in provenance. Findings are never hard-deleted — a
      superseded finding is retired via invalidated_at and superseded_by.
    category: architecture
    tags: [findings, provenance]
  - title: delapan_resume returns a preamble with a coverage verdict
    summary: The resume tool assembles a <preamble> from the KB's synopsis spine plus
      query-relevant findings banded by similarity, and returns a coverage verdict of
      rich, sparse, or gap. A gap verdict is the signal to run explore.
    category: behavior
    tags: [resume, preamble, coverage]
  - title: The explore pipeline fills knowledge gaps from the web
    summary: delapan_explore plans search queries, searches and crawls with Tavily,
      extracts findings with an LLM, scores their quality, merges near-duplicates,
      and persists the survivors to the KB. It needs AI_GATEWAY_API_KEY and
      TAVILY_API_KEY.
    category: behavior
    tags: [explore, pipeline]
  - title: Writes are self-correcting, not append-only
    summary: New findings resolve against the top-k most similar existing findings at
      write time. Each candidate is decided as ADD, UPDATE, NOOP, or SUPERSEDE, so
      the KB stays compact and contradictions retire the losing finding.
    category: architecture
    tags: [memory, dedup]
  - title: Two storage tiers at protocol parity
    summary: The local tier is SQLite + sqlite-vec with no account or cloud
      dependency; the cloud tier is Supabase (Postgres + pgvector). Both implement
      the same Store protocol with identical return shapes.
    category: architecture
    tags: [store, tiers]
  - title: The curation backlog ranks what the KB could not answer
    summary: Every resume/search coverage verdict is recorded. Gap and sparse queries
      aggregate into a backlog ranked by recurrence, severity, and recency —
      delapan_explore with no prompt consumes the top item.
    category: behavior
    tags: [curation, backlog]
  - title: Archiving is reversible, never destructive
    summary: delapan_archive stamps archived_at on a project or KB and touches no
      finding, node, or edge. Archived KBs stay readable, and exploring into one
      unarchives it.
    category: behavior
    tags: [archive, lifecycle]
  - title: The knowledge graph is co-designed through an intent schema
    summary: delapan_propose_kg_schema drafts a target ontology from the KB's
      findings; delapan_set_kg_schema persists the approved version; and
      delapan_build_graph extracts a graph steered by it, keeping intent and
      emergent structure comparable.
    category: behavior
    tags: [knowledge-graph, schema]
```

- [ ] **Step 2: Write the builder** — `scripts/build_demo_db.py`:

```python
"""Build data/demo.db — the bundled zero-key demo KB (maintainer-run).

    demo_findings.yaml ──► embed_batch ──► SQLiteStore(data/demo.db) ──► commit artifact

Usage: uv run --extra local python scripts/build_demo_db.py
Needs AI_GATEWAY_API_KEY (or OPENAI_API_KEY) in .env at build time only.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "demo.db"
SRC = REPO / "scripts" / "demo_findings.yaml"


async def main() -> None:
    from delapan.core.clients.embeddings import embed_batch
    from delapan.store.sqlite import SQLiteStore

    data = yaml.safe_load(SRC.read_text(encoding="utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()  # fully derived artifact — always rebuild from scratch

    store = SQLiteStore(str(OUT))
    org_id, project_id = store.resolve_project("delapan", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "demo", create=True)

    texts = [f"{f['title']}. {f['summary']}" for f in data["findings"]]
    embeddings = await embed_batch(texts)
    rows = [
        {
            "id": uuid.uuid4().hex[:8],
            "org_id": org_id,
            "kb_id": kb_id,
            "title": f["title"],
            "content": {"summary": f["summary"]},
            "category": f.get("category", "fact"),
            "confidence": 0.95,
            "tags": f.get("tags", []),
            "provenance": [],
            "embedding": emb,
        }
        for f, emb in zip(data["findings"], embeddings, strict=True)
    ]
    ids = await store.insert_findings(rows)
    store.upsert_synopsis(
        kb_id, data["synopsis"], finding_count=len(ids), model="handcrafted"
    )
    print(f"demo.db built: {len(ids)} findings, {len(data['synopsis'])} synopsis entries → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Un-ignore the artifact** — in `.gitignore`, directly below the `*.db` line add:

```
!data/demo.db
```

- [ ] **Step 4: Build the artifact**

Run: `uv run --extra local python scripts/build_demo_db.py`
Expected: `demo.db built: 9 findings, 6 synopsis entries → .../data/demo.db` (needs the maintainer's `.env` key; file lands under ~1 MB).

- [ ] **Step 5: Write the artifact test** — `tests/test_demo_db.py`:

```python
"""The committed demo artifact must stay loadable and populated (hermetic read)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo.db"


def test_demo_artifact_is_populated():
    from delapan.store.sqlite import SQLiteStore

    assert DEMO.exists(), "data/demo.db must be committed (scripts/build_demo_db.py)"
    store = SQLiteStore(str(DEMO))
    projects = store.list_projects(include_archived=True)
    demo = next(p for p in projects if p["project"] == "delapan")
    [kb] = demo["kbs"]  # the demo project has exactly one KB
    assert kb["finding_count"] >= 8

    syn = store.load_synopsis(kb["kb_id"])
    assert syn and len(syn["content"]) >= 4
    assert all({"topic", "gloss"} <= set(e) for e in syn["content"])
```

- [ ] **Step 6: Run the test**

Run: `uv run --extra local pytest tests/test_demo_db.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/demo_findings.yaml scripts/build_demo_db.py data/demo.db .gitignore tests/test_demo_db.py
git commit -m "feat: bundled demo KB (delapan/demo) + maintainer build script"
```

---

### Task 7: First-run demo seed on server start (TDD)

**Files:**
- Create: `tests/test_demo_seed.py`
- Modify: `delapan/mcp/onboarding.py` (add `seed_demo_if_absent`), `delapan/mcp/server.py:421-423` (`main`)

**Interfaces:**
- Consumes: `active_backend()`, `_default_db_path()` (honors `DELAPAN_DB_PATH`), `BUNDLED_DEMO_DB` (Task 3), `data/demo.db` (Task 6).
- Produces: `seed_demo_if_absent() -> None`, called once in `main()` before `mcp.run()`. Never runs on the cloud tier; never touches an existing DB.

- [ ] **Step 1: Write the failing tests** — `tests/test_demo_seed.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra local pytest tests/test_demo_seed.py -v`
Expected: FAIL — `ImportError: cannot import name 'seed_demo_if_absent'`.

- [ ] **Step 3: Implement** — append to `delapan/mcp/onboarding.py` (add `import shutil` up top):

```python
def seed_demo_if_absent() -> None:
    """First run on the local tier: copy the bundled demo KB into place.

    No-op whenever the target DB already exists, the bundled artifact is
    missing, or the active backend is the cloud tier."""
    from delapan.store import active_backend
    from delapan.store.sqlite import _default_db_path

    if active_backend() != "local":
        return
    target = Path(_default_db_path())
    if target.exists() or not BUNDLED_DEMO_DB.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUNDLED_DEMO_DB, target)
```

In `delapan/mcp/server.py`, import `seed_demo_if_absent` alongside `kb_not_found_card` and wire `main()`:

```python
def main() -> None:
    get_settings()  # fail fast if infra env is missing
    seed_demo_if_absent()
    mcp.run()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra local pytest tests/test_demo_seed.py tests/test_onboarding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add delapan/mcp/onboarding.py delapan/mcp/server.py tests/test_demo_seed.py
git commit -m "feat: seed bundled demo KB on first local server start"
```

---

### Task 8: README quickstart + docs sync

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: install/behavior facts from Tasks 1-7.
- Produces: the stranger-facing install story cited by the spec's acceptance criterion 1.

- [ ] **Step 1: Add the quickstart** — insert a section near the top of `README.md` (after the intro paragraph):

```markdown
## Install as a Claude Code plugin

Requires [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

    claude plugin marketplace add anthonysuherli/delapan
    claude plugin install delapan@delapan

First launch materializes the Python environment (via uv) and seeds a bundled
demo KB. **With zero keys configured** you can immediately run
`/delapan:projects` and `/delapan:resume` against the demo (project `delapan`,
kb `demo`). To unlock semantic search and web research on your own repos, copy
`.env.example` to `.env` in the plugin directory and set `AI_GATEWAY_API_KEY`
(plus `TAVILY_API_KEY` for `/delapan:explore`).

Skills: `/delapan:resume`, `/delapan:search`, `/delapan:explore`,
`/delapan:backlog`, `/delapan:projects`, `/delapan:model`.
```

- [ ] **Step 2: Sync the house-rule sections** — in the same file, update the "What's inside" table with rows for `scripts/mcp-server.sh` (plugin launcher), `skills/` (shipped Claude Code skills), `data/demo.db` (bundled demo KB), `delapan/mcp/onboarding.py` (first-run affordances); update "Status & roadmap" with a line: plugin shell shipped in-repo, marketplace-installable (2026-07-26).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: plugin quickstart + What's-inside/roadmap sync"
```

---

### Task 9: Stranger round-trip e2e + full-suite gate

**Files:**
- Create: `tests/test_stranger_e2e.py`

**Interfaces:**
- Consumes: everything shipped in Tasks 1-8 — the wrapper, the seeder, the onboarding card, the demo artifact; `mcp` Python client (`mcp>=1.16.0`, already a dependency).
- Produces: the executable form of spec acceptance criteria 1-2.

- [ ] **Step 1: Write the e2e** — `tests/test_stranger_e2e.py`:

```python
"""Stranger round-trip: cold-start the wrapper in a hermetic env, zero keys.

    tmp HOME ──► scripts/mcp-server.sh ──► seed demo ──► projects/resume over stdio
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "mcp-server.sh"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_stranger_round_trip(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),  # → ~/.delapan lands in tmp; seeder must fill it
        "CLAUDE_PLUGIN_ROOT": str(REPO),
        "DELAPAN_BACKEND": "local",
        # a stranger has no keys; force-empty so the maintainer's .env can't leak in
        "AI_GATEWAY_API_KEY": "",
        "OPENAI_API_KEY": "",
        "TAVILY_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        # reuse the real uv cache so the cold start doesn't re-download the world
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv")),
        "UV_PROJECT_ENVIRONMENT": str(REPO / ".venv"),
    }
    params = StdioServerParameters(command=str(WRAPPER), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            projects = _payload(await session.call_tool("delapan_projects", {}))
            assert any(p["project"] == "delapan" for p in projects["projects"]), (
                "demo KB must be seeded on first start"
            )

            resume = _payload(
                await session.call_tool(
                    "delapan_resume", {"project": "delapan", "kb": "demo"}
                )
            )
            assert "<synopsis>" in resume["preamble"], "zero-key resume must render the demo"

            card = _payload(
                await session.call_tool(
                    "delapan_resume", {"project": "ghost", "kb": "ghost"}
                )
            )
            assert "onboarding" in card and "try_demo" in card
```

- [ ] **Step 2: Run the e2e**

Run: `uv run --extra local pytest tests/test_stranger_e2e.py -v`
Expected: PASS (first run may take ~30s while uv resolves).

- [ ] **Step 3: Full-suite gate**

Run: `uv run --extra local pytest -x -q --ignore=tests/evals`
Expected: green except the **2 pre-existing env-related failures** documented on clean master — verify they are the same two by running the suite on `master` first if in doubt. Any new failure is a regression to fix before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_stranger_e2e.py
git commit -m "test: stranger round-trip e2e — cold wrapper start, demo seed, zero-key resume, onboarding card"
```

---

## Release checklist (after all tasks merge — operator steps, not plan tasks)

1. Merge `feat/plugin-release` → `master`; push (master is currently ahead of origin — reconcile first).
2. Bump `pyproject.toml` + `plugin.json` + `marketplace.json` versions together.
3. On a machine (or fresh user account) without the repo: `claude plugin marketplace add anthonysuherli/delapan` → install → run acceptance criteria 1-3 by hand.
4. Retire the old unversioned shell in `~/Repositories/8star/delapan-ai/` (keep `skills/tracking` locally).
