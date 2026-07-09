# delapan-82 OKF-Native Fork Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `8star/delapan-82` — a standalone fork of the delapan engine where the knowledge base is an in-repo Open Knowledge Format (OKF v0.1) bundle of markdown files, with a derived sqlite-vec index sidecar replacing the Store protocol.

**Architecture:** Files-canonical: `knowledge/` in the target repo IS the KB; a gitignored `.index/` sqlite-vec sidecar is derived from it and lazily revalidated by content hash. The exploration pipeline (plan→search→crawl→extract→evaluate→merge) copies over verbatim; only the persist boundary is new (`bundle/writer.py`, which also adds cross-run dedup). The preamble core (banding/coverage) ports unchanged with a markdown render target. Surface = 3 MCP tools + skills; no HTTP API, no Supabase, no frontend in v1.

**Tech Stack:** Python ≥3.11, pydantic v2 + pydantic-settings, PyYAML, sqlite-vec, OpenAI SDK (embeddings via Vercel AI Gateway), langchain-anthropic (synopsis LLM), tavily-python, FastMCP (`mcp` package), hatchling, pytest + pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-07-08-delapan-82-okf-native-design.md` (in the delapan backend repo; copied into the new repo in Task 1).

## Global Constraints

- New repo root: `/Users/anthonysuherli/Repositories/8star/delapan-82`. Source repo (read-only for copying): `/Users/anthonysuherli/Repositories/8star/delapan-ai/backend` (symlink to `~/projects/delapan`, branch `master`). Shell variables used throughout: `ROOT=/Users/anthonysuherli/Repositories/8star/delapan-82`, `SRC=/Users/anthonysuherli/Repositories/8star/delapan-ai/backend`.
- Package name `delapan82` (no hyphen). **Never** import `delapan.*` — the fork is fully standalone (br8n precedent).
- Config env override prefix `D82_<SECTION>__<FIELD>`; YAML path override env var `D82_CONFIG_FILE`. Secrets in `.env` via pydantic-settings; knobs in `config.yaml`. Never hardcode a knob.
- OKF v0.1 conformance for everything written: every non-reserved `.md` has parseable YAML frontmatter with non-empty `type`; reserved files `index.md`/`log.md` carry no frontmatter (exception: root `index.md` may carry only `okf_version: "0.1"`); consumers tolerate unknown types/keys/broken links and preserve unknown keys on round-trip; malformed files are skipped with a warning, never a crash.
- Bundle dir default `knowledge/` (knob `bundle.dir`). Derived index at `<bundle>/.index/` — gitignored, never source of truth.
- Embeddings: 1536-dim `text-embedding-3-small` via AI Gateway or OpenAI (config `embedding` section). Vectors never in files.
- House style: `from __future__ import annotations`, type hints throughout, terse module docstrings with an ASCII flow diagram, ruff line-length 100, `except Exception:  # noqa: BLE001 — <reason>` convention.
- Tests: pytest, `asyncio_mode = "auto"`, flat `tests/` layout, `tmp_path`-based fixtures, no network in tests (fake embedder fixture; LLM calls monkeypatched).
- Run tests/lint from `$ROOT` with `.venv/bin/pytest` and `.venv/bin/ruff check .`.
- Commit after every task (git repo created in Task 1, branch `main`). Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- **Scope note (from spec):** the KG builder / automated concept-doc synthesis is NOT ported in v1 — `concepts/` docs are agent/human-authored (the bundle + index treat them identically to findings). `deepen.py`, `state.py`, `concept_doc.py`, `clients/supabase.py`, `store/`, `api/` are not copied.

## File Structure (target repo)

```
delapan-82/
├─ pyproject.toml                      # package delapan82; deps trimmed (no fastapi/supabase)
├─ config.yaml                         # optional; defaults live in code
├─ .env.example                        # key names only
├─ .gitignore
├─ README.md                           # Task 12
├─ docs/specs/2026-07-08-delapan-82-okf-native-design.md   # copied spec
├─ mcp.json / .claude-plugin/ / skills/                    # Task 11 plugin shell
├─ delapan82/
│  ├─ core/
│  │  ├─ config.py                     # Task 1 — Settings + AppConfig (D82_ prefix)
│  │  ├─ clients/                      # Task 2 — copied: ai_gateway, anthropic, embeddings, tavily
│  │  ├─ exploration/                  # Task 2 — copied: engine, evaluator, extractor, merger, models, narrator, planner
│  │  └─ agent/
│  │     ├─ synopsis.py                # Task 8 — ported, file-backed
│  │     └─ preamble.py                # Task 9 — banding verbatim, markdown render
│  ├─ bundle/
│  │  ├─ hashing.py                    # Task 3 — fnv1a_hex
│  │  ├─ frontmatter.py                # Task 3 — parse_doc / serialize_doc
│  │  ├─ identity.py                   # Task 4 — slugify / new_id / unique_path
│  │  ├─ core.py                       # Task 5 — Doc, Bundle (discover/init/docs/read/write/log/index.md)
│  │  └─ writer.py                     # Task 7 — persist_findings + dedup (THE persist boundary)
│  ├─ index/
│  │  └─ sidecar.py                    # Task 6 — SidecarIndex (sqlite-vec, refresh/match/upsert)
│  └─ mcp/
│     ├─ banner.py                     # Task 10
│     └─ server.py                     # Task 10 — 3 tools
└─ tests/                              # flat; conftest.py has bundle + fake_embeddings fixtures
```

---

### Task 1: Repo bootstrap — pyproject, config module, skeleton

**Files:**
- Create: `$ROOT/` (git repo), `pyproject.toml`, `.gitignore`, `.env.example`, `delapan82/__init__.py`, `delapan82/core/__init__.py`, `delapan82/core/config.py`, `tests/__init__.py` (empty is fine to omit — flat tests need no package), `tests/test_config.py`, `docs/specs/` (copied spec)

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `delapan82.core.config` exporting `Settings`, `get_settings()`, `AppConfig`, `get_config()`, and section classes `AgentConfig`, `SearchConfig`, `TiersConfig`, `SynopsisConfig`, `ExplorationConfig`, `NarrationConfig`, `EmbeddingConfig`, `BundleConfig`, `DedupConfig`. Env prefix `D82_`, config file env `D82_CONFIG_FILE`. Later tasks import exactly these names.

- [ ] **Step 1: Create the repo and scaffolding**

```bash
ROOT=/Users/anthonysuherli/Repositories/8star/delapan-82
SRC=/Users/anthonysuherli/Repositories/8star/delapan-ai/backend
mkdir -p $ROOT && cd $ROOT && git init -b main
mkdir -p delapan82/core tests docs/specs
touch delapan82/__init__.py delapan82/core/__init__.py
cp $SRC/docs/superpowers/specs/2026-07-08-delapan-82-okf-native-design.md docs/specs/
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "delapan82"
version = "0.1.0"
description = "OKF-native grounding engine: the knowledge base is an Open Knowledge Format bundle living in your repo."
requires-python = ">=3.11"
license = { text = "AGPL-3.0-or-later" }
dependencies = [
  "pydantic>=2.12.5",
  "pydantic-settings>=2.12.0",
  "pyyaml>=6.0.3",
  "httpx>=0.28.1",
  "anthropic>=0.104.1",
  "langchain-core>=1.4.0",
  "langchain-anthropic>=1.4.3",
  "openai>=2.30.0",
  "tavily-python>=0.7.23",
  "mcp>=1.16.0",
  "sqlite-vec>=0.1.6",
]

[project.optional-dependencies]
dev = ["pytest>=8.4.2", "pytest-asyncio>=1.3.0", "ruff>=0.12.0", "pyright>=1.1.409"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["delapan82"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Note vs upstream: `sqlite-vec` is a core dependency (the sidecar is core now, not a "local tier"); `fastapi`, `uvicorn`, `jinja2` and the whole `cloud` extra are gone.

- [ ] **Step 3: Write `.gitignore` and `.env.example`**

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
.ruff_cache/
```

`.env.example`:
```
# LLM provider — any ONE of these unlocks the pipeline (gateway preferred)
AI_GATEWAY_API_KEY=
# AI_GATEWAY_BASE_URL=https://ai-gateway.vercel.sh/v1
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
# Web search for delapan_explore
TAVILY_API_KEY=
```

- [ ] **Step 4: Write the failing test** — `tests/test_config.py`

```python
from __future__ import annotations


def test_defaults_load_without_env(monkeypatch):
    monkeypatch.delenv("D82_CONFIG_FILE", raising=False)
    from delapan82.core.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    assert cfg.bundle.dir == "knowledge"
    assert cfg.dedup.threshold == 0.85
    assert cfg.tiers.band1_min == 0.55
    assert cfg.tiers.preamble_char_budget == 7000
    assert cfg.embedding.dim == 1536
    assert cfg.synopsis.rebuild_delta == 15


def test_env_override_uses_d82_prefix(monkeypatch):
    monkeypatch.setenv("D82_TIERS__BAND1_MIN", "0.7")
    monkeypatch.setenv("D82_BUNDLE__DIR", "kb")
    from delapan82.core.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    assert cfg.tiers.band1_min == 0.7
    assert cfg.bundle.dir == "kb"
    get_config.cache_clear()


def test_config_file_override(monkeypatch, tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("dedup:\n  threshold: 0.9\n", encoding="utf-8")
    monkeypatch.setenv("D82_CONFIG_FILE", str(f))
    from delapan82.core.config import get_config

    get_config.cache_clear()
    assert get_config().dedup.threshold == 0.9
    get_config.cache_clear()
```

- [ ] **Step 5: Create venv, install, run test to verify it fails**

```bash
cd $ROOT && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/test_config.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan82.core.config'`

- [ ] **Step 6: Write `delapan82/core/config.py`**

This is the upstream `delapan/core/config.py` with: prefix `DLP_`→`D82_`, file env `DELAPAN_CONFIG_FILE`→`D82_CONFIG_FILE`, Supabase/CORS/API-key/MCP-user/backend-selection settings deleted, sections `user_profile`/`deepen`/`knowledge_graph`/`concepts`/`prompts`/`okf` deleted, and new `BundleConfig`/`DedupConfig` sections added. Full file:

```python
"""Runtime configuration.

Two layers, kept deliberately separate:

* ``Settings`` — secrets and environment-bound infra (LLM/Tavily keys), read
  from process env / ``.env`` via pydantic-settings. All keys are optional:
  the engine can read a bundle with none of them set.
* ``AppConfig`` — tunable knobs (models, thresholds, budgets) from a
  human-editable YAML file.

``AppConfig`` precedence, lowest to highest:

    built-in defaults  <  config.yaml  <  environment variables

Environment overrides use the ``D82_<SECTION>__<FIELD>`` form, e.g.
``D82_TIERS__BAND1_MIN=0.7``. The YAML file defaults to the repo-root
``config.yaml``; point elsewhere with ``D82_CONFIG_FILE``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# =============================================================================
# Secrets / environment-bound settings
# =============================================================================


class Settings(BaseSettings):
    """Top-level secrets; values read from process env or `.env`.

    Everything is optional — the engine boots keyless for read-only bundle
    work; embeddings/search/explore degrade gracefully without keys.
    """

    # Resolve `.env` module-relative (repo-root `.env`) so it loads regardless
    # of launch CWD — the MCP server may be spawned from anywhere.
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore"
    )

    # LLM provider keys (optional)
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")

    # Vercel AI Gateway — routes the pipeline's LLM + embedding calls (optional).
    ai_gateway_api_key: str | None = Field(default=None, alias="AI_GATEWAY_API_KEY")
    ai_gateway_base_url: str = Field(
        default="https://ai-gateway.vercel.sh/v1", alias="AI_GATEWAY_BASE_URL"
    )

    # Exploration tooling — web search/extract.
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# =============================================================================
# Tunable application config (YAML-backed)
# =============================================================================


class AgentConfig(BaseModel):
    """LLM client defaults (chat_model falls back to these when no model given)."""

    model: str = "claude-sonnet-4-6"
    fast_model: str = "claude-haiku-4-5"
    temperature: float = 0.2
    max_tokens: int = 4096
    thinking_budget: int = 0
    max_tool_iterations: int = 4
    max_messages_per_turn: int = 40


class BundleConfig(BaseModel):
    """Where the OKF bundle lives inside a repo."""

    dir: str = "knowledge"  # bundle directory name, resolved upward from CWD


class DedupConfig(BaseModel):
    """Cross-run finding dedup at the persist boundary."""

    threshold: float = 0.85  # cosine sim vs existing finding docs; >= merges


class SearchConfig(BaseModel):
    """Recall + shared retrieval knobs."""

    default_limit: int = 10
    max_limit: int = 50
    min_similarity: float = 0.0


class TiersConfig(BaseModel):
    """Similarity-band thresholds + always-on preamble budget."""

    band1_min: float = 0.55  # strong-match floor (always injected)
    band2_min: float = 0.40  # moderate
    band3_min: float = 0.25  # weak / peripheral floor
    rich_hit_count: int = 3  # band-1 hits => coverage="rich"
    preamble_char_budget: int = 7000  # max chars of the assembled preamble
    excerpt_char_cap: int = 1200  # per-doc body truncation in the preamble


class SynopsisConfig(BaseModel):
    """Per-KB synopsis spine: build model + incremental regen triggers."""

    model: str = "claude-haiku-4-5"
    rebuild_delta: int = 15  # findings added since build => regen
    rebuild_max_age_hours: int = 168
    max_entries: int = 6


class ExplorationConfig(BaseModel):
    """Research pipeline knobs. Models are AI Gateway provider/model dot slugs."""

    planner_model: str = "anthropic/claude-sonnet-4.6"
    extraction_model: str = "anthropic/claude-sonnet-4.6"
    extraction_fallback_model: str = "openai/gpt-5.4-mini"
    temperature: float = 0.0
    reasoning_effort: str | None = None  # "low" | "medium" | "high"

    search_mode: str = "auto"

    @field_validator("search_mode")
    @classmethod
    def _check_search_mode(cls, v: str) -> str:
        if v not in {"auto", "agent", "tavily"}:
            raise ValueError(f"search_mode must be auto|agent|tavily, got {v!r}")
        return v

    search_depth: str = "advanced"
    max_results_per_query: int = 20
    fallback_result_threshold: int = 5
    expansion_result_threshold: int = 3
    max_concurrent_searches: int = 6

    max_pages: int = 15
    max_concurrent_extractions: int = 10
    max_content_per_page: int = 100_000

    enable_evaluation: bool = True
    evaluation_model: str = "anthropic/claude-sonnet-4.6"

    fuzzy_match_threshold: float = 0.80
    min_confidence_threshold: float = 0.2

    default_max_findings: int = 12
    max_findings: int = 40


class NarrationConfig(BaseModel):
    """Per-phase narration: a cheap gateway line per pipeline phase (best-effort)."""

    enabled: bool = True
    model: str = "google/gemini-2.5-flash"
    temperature: float = 0.3
    max_tokens: int = 60


class EmbeddingConfig(BaseModel):
    """Embedding client knobs."""

    model: str = "text-embedding-3-small"
    dim: int = 1536
    input_char_cap: int = 8192


class AppConfig(BaseModel):
    """Aggregate of all tunable sections."""

    agent: AgentConfig = Field(default_factory=AgentConfig)
    bundle: BundleConfig = Field(default_factory=BundleConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    tiers: TiersConfig = Field(default_factory=TiersConfig)
    synopsis: SynopsisConfig = Field(default_factory=SynopsisConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    narration: NarrationConfig = Field(default_factory=NarrationConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


# -----------------------------------------------------------------------------
# Loader
# -----------------------------------------------------------------------------

_ENV_PREFIX = "D82_"
_NESTED_DELIM = "__"


def _config_path() -> Path:
    override = os.getenv("D82_CONFIG_FILE")
    if override:
        return Path(override)
    # config.py lives at <root>/delapan82/core/config.py → <root>/config.yaml
    return Path(__file__).resolve().parents[2] / "config.yaml"


def _load_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping at the top level")
    return data


def _env_overrides() -> dict:
    """Collect `D82_<SECTION>__<FIELD>` env vars into a nested dict."""
    out: dict[str, dict] = {}
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX) or _NESTED_DELIM not in key:
            continue
        section, _, field = key[len(_ENV_PREFIX) :].partition(_NESTED_DELIM)
        if not section or not field:
            continue
        out.setdefault(section.lower(), {})[field.lower()] = value
    return out


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Load the layered app config: defaults < config.yaml < env vars. Cached."""
    data: dict = {}
    _deep_merge(data, _load_file(_config_path()))
    _deep_merge(data, _env_overrides())
    return AppConfig.model_validate(data)
```

Gotcha: `test_env_override_uses_d82_prefix` reads `D82_CONFIG_FILE` from a prior test's env unless each test clears it — `monkeypatch` auto-reverts env per test, and each test calls `get_config.cache_clear()`, so ordering is safe.

- [ ] **Step 7: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_config.py -v
```
Expected: 3 passed

- [ ] **Step 8: Lint and commit**

```bash
.venv/bin/ruff check . && git add -A && git commit -m "feat: bootstrap delapan82 package — config module (D82_ prefix, bundle/dedup sections)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Copy the clients + exploration pipeline (verbatim, imports renamed)

**Files:**
- Create (copied): `delapan82/core/clients/{__init__,ai_gateway,anthropic,embeddings,tavily}.py`, `delapan82/core/exploration/{__init__,engine,evaluator,extractor,merger,models,narrator,planner}.py`, `delapan82/core/agent/__init__.py` (empty)
- Test: `tests/test_merger.py`

**Interfaces:**
- Consumes: `delapan82.core.config` (Task 1) — copied modules import `get_config`, `get_settings`, `ExplorationConfig`, `SynopsisConfig`, `TiersConfig`, `AgentConfig`, `EmbeddingConfig`; all exist after Task 1.
- Produces: `run_exploration(prompt, *, exploration_id, project_id, kb_id, cfg, on_progress=None, on_narration=None, lens="explore") -> list[Finding]`; `Finding` pydantic model (fields: `id, exploration_id, project_id, kb, category, title, content: dict, confidence, quality, source_count, provenance, extraction_model, created_at, updated_at, tags, entity_type, relationships, layout_hint`); `FindingMerger`, `confidence_from_sources(n)`, `blended_confidence(n, quality)`; `embed_text(text) -> list[float]`, `embed_batch(texts) -> list[list[float]]`; `chat_model(model)`.

NOT copied (deliberate, per spec): `deepen.py` (dead), `clients/supabase.py`, `core/agent/state.py`, `core/agent/concept_doc.py` (KG-coupled; concept synthesis is a follow-on), `store/`, `api/`, `core/knowledge_graph/`.

- [ ] **Step 1: Copy the modules**

```bash
cd $ROOT
mkdir -p delapan82/core/clients delapan82/core/exploration delapan82/core/agent
touch delapan82/core/agent/__init__.py
for f in __init__ ai_gateway anthropic embeddings tavily; do
  cp $SRC/delapan/core/clients/$f.py delapan82/core/clients/; done
for f in __init__ engine evaluator extractor merger models narrator planner; do
  cp $SRC/delapan/core/exploration/$f.py delapan82/core/exploration/; done
```

- [ ] **Step 2: Rename imports `delapan.` → `delapan82.`**

```bash
LC_ALL=C find delapan82 -name '*.py' -exec sed -i '' \
  -e 's/from delapan\./from delapan82./g' -e 's/import delapan\./import delapan82./g' {} +
grep -rn "delapan\." delapan82 --include='*.py' | grep -v "delapan82\." | grep -v "^Binary" || echo CLEAN
```
Expected: `CLEAN` (remaining hits, if any, are prose in docstrings — acceptable, but there must be no `from delapan.` / `import delapan.` code lines). Also edit `delapan82/core/clients/__init__.py`'s docstring: delete the paragraph about Supabase/PostHog and the `delapan.store` seam (that seam no longer exists).

- [ ] **Step 3: Verify everything imports**

```bash
.venv/bin/python -c "from delapan82.core.exploration import run_exploration, Finding; from delapan82.core.clients.embeddings import embed_text, embed_batch; from delapan82.core.clients.anthropic import chat_model; print('imports OK')"
```
Expected: `imports OK`. If `clients/__init__.py` re-exports the deleted supabase client, remove that import line from it.

- [ ] **Step 4: Write the test** — `tests/test_merger.py`

```python
from __future__ import annotations

from delapan82.core.exploration.merger import (
    FindingMerger,
    blended_confidence,
    confidence_from_sources,
)
from delapan82.core.exploration.models import Finding


def _f(title: str, urls: list[str], category: str = "fact") -> Finding:
    return Finding(
        exploration_id="e1",
        project_id="p1",
        category=category,
        title=title,
        content={"summary": title},
        provenance=[{"url": u} for u in urls],
    )


def test_confidence_curve():
    assert round(confidence_from_sources(1), 2) == 0.40
    assert round(confidence_from_sources(2), 2) == 0.64
    assert round(blended_confidence(2, 0.5), 2) == 0.32


def test_merger_clusters_similar_titles():
    merger = FindingMerger(fuzzy_threshold=0.80, min_confidence=0.2)
    merged = merger.merge_findings(
        [_f("HK IFRS 17 adoption timeline", ["http://a"]),
         _f("HK IFRS 17 adoption timelines", ["http://b"]),
         _f("Something entirely different", ["http://c"])]
    )
    assert len(merged) == 2
    two_source = [m for m in merged if m.source_count == 2]
    assert len(two_source) == 1


def test_merger_drops_low_confidence():
    merger = FindingMerger(fuzzy_threshold=0.80, min_confidence=0.5)
    merged = merger.merge_findings([_f("Lone weak finding", ["http://a"])])
    assert merged == []  # 1 source => 0.40 < 0.5 floor
```

- [ ] **Step 5: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/ -v && .venv/bin/ruff check .
git add -A && git commit -m "feat: port exploration pipeline + LLM/search clients from delapan (imports renamed)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: all tests pass (test_config + test_merger).

---

### Task 3: Frontmatter round-trip + hashing

**Files:**
- Create: `delapan82/bundle/__init__.py` (empty), `delapan82/bundle/hashing.py`, `delapan82/bundle/frontmatter.py`
- Test: `tests/test_frontmatter.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `fnv1a_hex(text: str) -> str` (8-char hex, FNV-1a 32-bit — same algorithm as upstream `grounded_hash`); `parse_doc(text: str) -> tuple[dict, str] | None` (meta, body; None = not a valid OKF doc); `serialize_doc(meta: dict, body: str) -> str`.

- [ ] **Step 1: Write the failing tests** — `tests/test_frontmatter.py`

```python
from __future__ import annotations

from delapan82.bundle.frontmatter import parse_doc, serialize_doc
from delapan82.bundle.hashing import fnv1a_hex


def test_fnv1a_matches_upstream_grounded_hash():
    # Upstream grounded_hash(["b", "a"]) hashes "a,b"; fnv1a_hex is the same
    # primitive over an arbitrary string.
    assert fnv1a_hex("a,b") == _reference_fnv("a,b")
    assert len(fnv1a_hex("anything")) == 8


def _reference_fnv(key: str) -> str:
    h = 0x811C9DC5
    for b in key.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def test_parse_valid_doc():
    text = '---\ntype: finding\ntitle: "T"\n---\n\nBody **here**.\n'
    meta, body = parse_doc(text)
    assert meta["type"] == "finding" and meta["title"] == "T"
    assert body == "Body **here**."  # body is newline-normalized (see parse_doc)


def test_parse_rejects_missing_or_broken_frontmatter():
    assert parse_doc("just markdown, no frontmatter") is None
    assert parse_doc("---\ntype: finding\nno closing delimiter") is None
    assert parse_doc("---\n- a\n- b\n---\nbody") is None  # non-mapping
    assert parse_doc("---\n{unparseable: [yaml\n---\nbody") is None


def test_roundtrip_preserves_unknown_keys():
    meta = {"type": "finding", "title": "T", "x_custom": {"a": 1}, "tags": ["p", "q"]}
    text = serialize_doc(meta, "Body.")
    meta2, body2 = parse_doc(text)
    assert meta2 == meta
    assert body2.rstrip() == "Body."


def test_roundtrip_unicode():
    meta = {"type": "concept", "title": "延べ払い — deferral"}
    meta2, _ = parse_doc(serialize_doc(meta, "本文"))
    assert meta2["title"] == "延べ払い — deferral"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_frontmatter.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan82.bundle'`

- [ ] **Step 3: Write the implementation**

`delapan82/bundle/hashing.py`:
```python
"""Content hashing for the derived index — FNV-1a 32-bit (upstream grounded_hash
primitive), stable across processes and platforms.

    text ─► fnv1a_hex ─► 8-char hex
"""

from __future__ import annotations


def fnv1a_hex(text: str) -> str:
    """FNV-1a 32-bit hex over the UTF-8 bytes of `text`."""
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"
```

`delapan82/bundle/frontmatter.py`:
```python
"""OKF frontmatter codec: YAML block + markdown body, unknown keys preserved.

    text ─► parse_doc ─► (meta, body) ─► serialize_doc ─► text
"""

from __future__ import annotations

import yaml

_DELIM = "---"


def parse_doc(text: str) -> tuple[dict, str] | None:
    """Split a document into (frontmatter mapping, body).

    Returns None when the document is not a valid OKF concept doc (no leading
    delimiter, unclosed block, unparseable YAML, or non-mapping frontmatter).
    Callers skip-and-warn on None — per spec, consumers never crash on one
    malformed file."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIM:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            try:
                meta = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                return None
            if not isinstance(meta, dict):
                return None
            # Normalize leading/trailing newlines: serialize_doc adds them, and
            # embedding/hashing must see the same body a fresh query would.
            body = "\n".join(lines[i + 1 :]).strip("\n")
            return meta, body
    return None


def serialize_doc(meta: dict, body: str) -> str:
    """Render (meta, body) to OKF text. Key order is preserved (insertion
    order), values round-trip through yaml.safe_dump."""
    fm = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip("\n")
    return f"{_DELIM}\n{fm}\n{_DELIM}\n\n{body.rstrip()}\n"
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_frontmatter.py -v && .venv/bin/ruff check .
git add -A && git commit -m "feat: OKF frontmatter codec + FNV-1a content hashing

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 5 passed.

---

### Task 4: Identity — slugs, ids, collision-free paths

**Files:**
- Create: `delapan82/bundle/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `new_id() -> str` (uuid4 hex[:8], matches `Finding.id` style); `slugify(title: str) -> str`; `unique_path(directory: Path, slug: str) -> Path` (appends `-2`, `-3`… on collision).

- [ ] **Step 1: Write the failing tests** — `tests/test_identity.py`

```python
from __future__ import annotations

from delapan82.bundle.identity import new_id, slugify, unique_path


def test_new_id_shape():
    a, b = new_id(), new_id()
    assert len(a) == 8 and a != b and all(c in "0123456789abcdef" for c in a)


def test_slugify():
    assert slugify("HK IFRS 17: Adoption & Timeline!") == "hk-ifrs-17-adoption-timeline"
    assert slugify("  ---  ") == "untitled"
    assert len(slugify("x" * 300)) <= 60
    assert slugify("Éclair über café") == "eclair-uber-cafe"


def test_unique_path(tmp_path):
    p1 = unique_path(tmp_path, "note")
    p1.write_text("x")
    p2 = unique_path(tmp_path, "note")
    assert p1.name == "note.md" and p2.name == "note-2.md"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_identity.py -v
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write the implementation** — `delapan82/bundle/identity.py`

```python
"""File identity: human slugs for paths, immutable ids in frontmatter.

    title ─► slugify ─► findings/<slug>.md ; identity = frontmatter `id`
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from uuid import uuid4

_MAX_SLUG = 60


def new_id() -> str:
    """8-char uuid4 hex — the same shape as the pipeline's Finding.id."""
    return uuid4().hex[:8]


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:_MAX_SLUG].rstrip("-") or "untitled"


def unique_path(directory: Path, slug: str) -> Path:
    """First free `<slug>.md`, `<slug>-2.md`, … in `directory`."""
    p = directory / f"{slug}.md"
    n = 2
    while p.exists():
        p = directory / f"{slug}-{n}.md"
        n += 1
    return p
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_identity.py -v && .venv/bin/ruff check .
git add -A && git commit -m "feat: slug/id identity scheme for bundle files

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 3 passed.

---

### Task 5: Bundle core — discover, init, read/write, log, index.md

**Files:**
- Create: `delapan82/bundle/core.py`
- Test: `tests/test_bundle_core.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `parse_doc`/`serialize_doc` (Task 3), `get_config().bundle.dir` (Task 1).
- Produces:
  - `Doc` dataclass: `path: Path`, `rel: str` (bundle-relative posix), `meta: dict`, `body: str`
  - `Bundle(root: Path)` with: `Bundle.discover(start: Path | None = None) -> Bundle | None`; `Bundle.init(root: Path) -> Bundle`; `docs(type: str | None = None) -> Iterator[Doc]`; `read(rel: str) -> Doc | None`; `write(rel: str, meta: dict, body: str) -> Path` (atomic; raises `ValueError` on empty `type`); `append_log(entries: list[str], day: str) -> None`; `rebuild_index_md() -> None`
  - `RESERVED = {"index.md", "log.md"}`

- [ ] **Step 1: Write `tests/conftest.py`** (bundle fixture; the fake-embeddings fixture is added in Task 6)

```python
from __future__ import annotations

import pytest


@pytest.fixture()
def bundle(tmp_path):
    from delapan82.bundle.core import Bundle

    return Bundle.init(tmp_path / "knowledge")
```

- [ ] **Step 2: Write the failing tests** — `tests/test_bundle_core.py`

```python
from __future__ import annotations

import pytest

from delapan82.bundle.core import Bundle


def test_init_creates_okf_skeleton(bundle):
    root = bundle.root
    assert (root / "index.md").exists() and (root / "log.md").exists()
    assert (root / ".gitignore").read_text() == ".index/\n"
    assert 'okf_version: "0.1"' in (root / "index.md").read_text()


def test_discover_walks_up_and_stops_at_git_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    Bundle.init(repo / "knowledge")
    deep = repo / "src" / "nested"
    deep.mkdir(parents=True)
    found = Bundle.discover(start=deep)
    assert found is not None and found.root == repo / "knowledge"
    # no bundle anywhere → None (never escapes past the git root)
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    assert Bundle.discover(start=bare) is None


def test_write_requires_type_and_is_readable(bundle):
    with pytest.raises(ValueError):
        bundle.write("findings/x.md", {"title": "no type"}, "b")
    bundle.write("findings/x.md", {"id": "a1", "type": "finding", "title": "X"}, "Body.")
    doc = bundle.read("findings/x.md")
    assert doc.meta["title"] == "X" and doc.body.rstrip() == "Body."


def test_docs_skips_reserved_and_malformed(bundle):
    bundle.write("findings/good.md", {"id": "a1", "type": "finding", "title": "G"}, "g")
    (bundle.root / "findings" / "bad.md").write_text("no frontmatter at all")
    (bundle.root / ".index").mkdir(exist_ok=True)
    (bundle.root / ".index" / "stray.md").write_text("---\ntype: x\n---\nb")
    rels = [d.rel for d in bundle.docs()]
    assert rels == ["findings/good.md"]
    assert [d.rel for d in bundle.docs(type="concept")] == []


def test_append_log_groups_by_day(bundle):
    bundle.append_log(["**Creation**: [A](findings/a.md)"], day="2026-07-09")
    bundle.append_log(["**Update**: [B](findings/b.md)"], day="2026-07-09")
    text = (bundle.root / "log.md").read_text()
    assert text.count("## 2026-07-09") == 1
    assert text.index("Update") < text.index("Creation")  # newest first within day


def test_rebuild_index_md_lists_docs_grouped_by_type(bundle):
    bundle.write(
        "findings/a.md",
        {"id": "a1", "type": "finding", "title": "Alpha", "description": "first fact"},
        "b",
    )
    bundle.write("concepts/c.md", {"id": "c1", "type": "concept", "title": "Gamma"}, "b")
    bundle.rebuild_index_md()
    text = (bundle.root / "index.md").read_text()
    assert "* [Alpha](findings/a.md) - first fact" in text
    assert "* [Gamma](concepts/c.md)" in text
    assert text.startswith("---\nokf_version:")
```

- [ ] **Step 3: Run to verify failure**

```bash
.venv/bin/pytest tests/test_bundle_core.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan82.bundle.core'`

- [ ] **Step 4: Write the implementation** — `delapan82/bundle/core.py`

```python
"""The OKF bundle — the knowledge base IS this directory of markdown files.

    repo/knowledge/           ← Bundle.root (source of truth, git-tracked)
      index.md  log.md        ← reserved (OKF v0.1)
      findings/*.md           ← type: finding
      concepts/*.md           ← type: concept
      .index/                 ← derived sidecar (gitignored, never truth)

Everything the engine writes conforms to OKF v0.1: frontmatter with a
non-empty `type`, unknown keys preserved, reserved files bare. Reads are
permissive: malformed files are skipped with a warning, never a crash.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from delapan82.bundle.frontmatter import parse_doc, serialize_doc
from delapan82.core.config import get_config

logger = logging.getLogger(__name__)

RESERVED = {"index.md", "log.md"}

_INDEX_SEED = '---\nokf_version: "0.1"\n---\n\n# knowledge bundle\n'


@dataclass
class Doc:
    path: Path
    rel: str
    meta: dict
    body: str


class Bundle:
    def __init__(self, root: Path) -> None:
        self.root = root

    # --- location ------------------------------------------------------------

    @classmethod
    def discover(cls, start: Path | None = None) -> Bundle | None:
        """Walk up from `start` (default CWD) to the git root looking for the
        configured bundle dir. None when the repo has no bundle yet."""
        cur = (start or Path.cwd()).resolve()
        dirname = get_config().bundle.dir
        for p in [cur, *cur.parents]:
            cand = p / dirname
            if cand.is_dir():
                return cls(cand)
            if (p / ".git").exists():
                break  # repo root reached without a bundle
        return None

    @classmethod
    def init(cls, root: Path) -> Bundle:
        """Create the OKF skeleton (idempotent)."""
        root.mkdir(parents=True, exist_ok=True)
        if not (root / "index.md").exists():
            (root / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
        if not (root / "log.md").exists():
            (root / "log.md").write_text("", encoding="utf-8")
        if not (root / ".gitignore").exists():
            (root / ".gitignore").write_text(".index/\n", encoding="utf-8")
        (root / "findings").mkdir(exist_ok=True)
        (root / "concepts").mkdir(exist_ok=True)
        return cls(root)

    # --- documents -------------------------------------------------------------

    def docs(self, type: str | None = None) -> Iterator[Doc]:
        """All concept documents, skipping reserved files, .index/, and
        malformed docs (warned, per OKF consumer permissiveness)."""
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            if path.name in RESERVED or rel.startswith(".index/"):
                continue
            doc = self.read(rel)
            if doc is None:
                logger.warning("skipping malformed OKF doc: %s", rel)
                continue
            if type is not None and doc.meta.get("type") != type:
                continue
            yield doc

    def read(self, rel: str) -> Doc | None:
        path = self.root / rel
        if not path.is_file():
            return None
        parsed = parse_doc(path.read_text(encoding="utf-8"))
        if parsed is None:
            return None
        meta, body = parsed
        return Doc(path=path, rel=rel, meta=meta, body=body)

    def write(self, rel: str, meta: dict, body: str) -> Path:
        """Atomic write (temp + rename). Producer-side conformance: `type`
        must be a non-empty string."""
        if not str(meta.get("type", "")).strip():
            raise ValueError(f"OKF doc {rel!r} requires a non-empty 'type' in frontmatter")
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialize_doc(meta, body))
        os.replace(tmp, path)
        return path

    # --- reserved files ----------------------------------------------------------

    def append_log(self, entries: list[str], day: str) -> None:
        """Prepend entries under a `## <day>` heading (newest first, per OKF)."""
        if not entries:
            return
        path = self.root / "log.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        bullets = "\n".join(f"* {e}" for e in entries)
        head = f"## {day}\n"
        if existing.startswith(head):
            content = f"{head}{bullets}\n{existing[len(head):]}"
        else:
            content = f"{head}{bullets}\n" + (f"\n{existing}" if existing.strip() else "")
        path.write_text(content.rstrip() + "\n", encoding="utf-8")

    def rebuild_index_md(self) -> None:
        """Regenerate the root index.md listing, grouped by `type`. Entry
        descriptions come from frontmatter, per OKF index convention."""
        groups: dict[str, list[Doc]] = {}
        for d in self.docs():
            groups.setdefault(str(d.meta.get("type", "other")), []).append(d)
        lines = ['---', 'okf_version: "0.1"', '---', '', '# knowledge bundle', '']
        for t in sorted(groups):
            lines.append(f"## {t}")
            for d in sorted(groups[t], key=lambda x: str(x.meta.get("title", x.rel))):
                title = str(d.meta.get("title", d.rel))
                desc = str(d.meta.get("description", "")).strip()
                lines.append(f"* [{title}]({d.rel})" + (f" - {desc}" if desc else ""))
            lines.append("")
        (self.root / "index.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
```

- [ ] **Step 5: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_bundle_core.py -v && .venv/bin/ruff check .
git add -A && git commit -m "feat: Bundle core — OKF directory ops (discover/init/read/write/log/index)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 6 passed.

---

### Task 6: Sidecar index — sqlite-vec, refresh (invalidation), match

**Files:**
- Create: `delapan82/index/__init__.py` (empty), `delapan82/index/sidecar.py`
- Modify: `tests/conftest.py` (add `fake_embeddings` fixture)
- Test: `tests/test_sidecar.py`

**Interfaces:**
- Consumes: `Bundle`/`Doc` (Task 5), `fnv1a_hex` (Task 3), `embed_batch`/`embed_text` (Task 2), `get_config().embedding.dim`.
- Produces: `SidecarIndex(bundle)` with:
  - `async refresh() -> dict` — `{"embedded": int, "removed": int, "total": int}`; adopts new files, re-embeds hash-changed ones, drops deleted, re-associates renames by frontmatter `id` without re-embedding
  - `match(query_embedding, *, count: int, min_similarity: float) -> list[dict]` — rows `{"id", "rel", "type", "title", "category", "content", "similarity"}` where `content` is the CURRENT file body (read path resolves to files)
  - `upsert(rel: str, meta: dict, body: str, embedding: list[float]) -> None` — direct insert used by the writer to avoid double-embedding
- Doc identity in the index: frontmatter `id` when present, else `fnv1a_hex(rel)`.

- [ ] **Step 1: Add the fake-embeddings fixture to `tests/conftest.py`** (append to the existing file)

```python
@pytest.fixture()
def fake_embeddings(monkeypatch):
    """Deterministic, network-free embeddings: sha256 bytes tiled to 1536 dims.
    Identical text → cosine 1.0; different text → low similarity."""
    import hashlib
    import importlib

    def _vec(text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [((b / 255.0) * 2.0 - 1.0) for b in h] * 48  # 32 * 48 = 1536

    async def fake_embed_text(text: str) -> list[float]:
        return _vec(text)

    async def fake_embed_batch(texts):
        return [_vec(t) for t in texts]

    for name in (
        "delapan82.index.sidecar",
        "delapan82.bundle.writer",
        "delapan82.core.agent.preamble",
        "delapan82.mcp.server",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue  # module arrives in a later task — fixture stays usable now
        monkeypatch.setattr(mod, "embed_text", fake_embed_text, raising=False)
        monkeypatch.setattr(mod, "embed_batch", fake_embed_batch, raising=False)
    return _vec
```

(Modules are imported via `importlib` and skipped when absent — a plain
`monkeypatch.setattr("pkg.mod.attr", ...)` would raise `ModuleNotFoundError`
for modules that only exist after Tasks 7/9/10. `delapan82.mcp.server` is in
the list because it imports `embed_text` into its own namespace.)

- [ ] **Step 2: Write the failing tests** — `tests/test_sidecar.py`

```python
from __future__ import annotations

from delapan82.index.sidecar import SidecarIndex


def _seed(bundle, rel: str, title: str, body: str, id_: str) -> None:
    bundle.write(rel, {"id": id_, "type": "finding", "title": title, "category": "fact"}, body)


async def test_refresh_adopts_edits_and_deletes(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    _seed(bundle, "findings/a.md", "Alpha", "alpha body", "a1")
    _seed(bundle, "findings/b.md", "Beta", "beta body", "b1")
    stats = await idx.refresh()
    assert stats == {"embedded": 2, "removed": 0, "total": 2}

    # idempotent — nothing re-embedded on a clean second pass
    assert (await idx.refresh())["embedded"] == 0

    # human edit → re-embedded
    _seed(bundle, "findings/a.md", "Alpha", "alpha body EDITED", "a1")
    assert (await idx.refresh())["embedded"] == 1

    # delete → removed
    (bundle.root / "findings" / "b.md").unlink()
    stats = await idx.refresh()
    assert stats["removed"] == 1 and stats["total"] == 1


async def test_rename_reassociates_by_id_without_reembed(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    _seed(bundle, "findings/old-name.md", "Alpha", "alpha body", "a1")
    await idx.refresh()
    (bundle.root / "findings" / "old-name.md").rename(bundle.root / "findings" / "new-name.md")
    stats = await idx.refresh()
    assert stats["embedded"] == 0 and stats["total"] == 1
    hits = idx.match(fake_embeddings("alpha body"), count=5, min_similarity=0.0)
    assert hits[0]["rel"] == "findings/new-name.md"


async def test_match_returns_fresh_body_and_similarity(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    _seed(bundle, "findings/a.md", "Alpha", "alpha body", "a1")
    await idx.refresh()
    hits = idx.match(fake_embeddings("alpha body"), count=5, min_similarity=0.0)
    assert len(hits) == 1
    h = hits[0]
    assert h["similarity"] > 0.99
    assert h["content"] == "alpha body"
    assert h["title"] == "Alpha" and h["type"] == "finding"
    # min_similarity filters
    assert idx.match(fake_embeddings("zzz unrelated"), count=5, min_similarity=0.99) == []
```

- [ ] **Step 3: Run to verify failure**

```bash
.venv/bin/pytest tests/test_sidecar.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan82.index.sidecar'`

- [ ] **Step 4: Write the implementation** — `delapan82/index/sidecar.py`

```python
"""Derived vector index over an OKF bundle — never source of truth.

    bundle files ─► refresh (hash-diff) ─► docs + vec_docs (sqlite-vec)
    query vec ─► KNN ─► resolve rows back to FILES ─► fresh bodies

Lives at <bundle>/.index/index.db (gitignored). Deleting it is always safe;
the next refresh rebuilds from files.
"""

from __future__ import annotations

import sqlite3

import sqlite_vec
from sqlite_vec import serialize_float32

from delapan82.bundle.core import Bundle, Doc
from delapan82.bundle.hashing import fnv1a_hex
from delapan82.core.clients.embeddings import embed_batch, embed_text  # noqa: F401 — embed_text re-exported for callers/tests
from delapan82.core.config import get_config


def _doc_id(doc: Doc) -> str:
    return str(doc.meta.get("id") or fnv1a_hex(doc.rel))


class SidecarIndex:
    def __init__(self, bundle: Bundle) -> None:
        self._bundle = bundle
        idx_dir = bundle.root / ".index"
        idx_dir.mkdir(exist_ok=True)
        self._conn = sqlite3.connect(idx_dir / "index.db")
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        dim = get_config().embedding.dim
        self._conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS docs (
              id TEXT PRIMARY KEY,
              rel TEXT NOT NULL UNIQUE,
              type TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT '',
              body_hash TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs
              USING vec0(doc_id TEXT, embedding float[{dim}]);
            """
        )

    def upsert(self, rel: str, meta: dict, body: str, embedding: list[float]) -> None:
        """Direct insert/replace with a caller-supplied vector (the writer uses
        this to avoid embedding the same body twice)."""
        doc_id = str(meta.get("id") or fnv1a_hex(rel))
        self._conn.execute("DELETE FROM vec_docs WHERE doc_id = ?;", (doc_id,))
        self._conn.execute(
            "INSERT OR REPLACE INTO docs (id, rel, type, title, category, body_hash)"
            " VALUES (?,?,?,?,?,?);",
            (
                doc_id,
                rel,
                str(meta.get("type", "")),
                str(meta.get("title", "")),
                str(meta.get("category", "")),
                fnv1a_hex(body),
            ),
        )
        self._conn.execute(
            "INSERT INTO vec_docs (doc_id, embedding) VALUES (?, ?);",
            (doc_id, serialize_float32(list(embedding))),
        )
        self._conn.commit()

    async def refresh(self) -> dict:
        """Converge the index onto the current files: adopt new, re-embed
        changed (by body hash), drop deleted, re-associate renames by id."""
        current: dict[str, Doc] = {}
        for doc in self._bundle.docs():
            current[_doc_id(doc)] = doc

        rows = {
            r[0]: (r[1], r[2])
            for r in self._conn.execute("SELECT id, rel, body_hash FROM docs;")
        }

        removed = [doc_id for doc_id in rows if doc_id not in current]
        for doc_id in removed:
            self._conn.execute("DELETE FROM docs WHERE id = ?;", (doc_id,))
            self._conn.execute("DELETE FROM vec_docs WHERE doc_id = ?;", (doc_id,))

        stale: list[tuple[str, Doc]] = []
        for doc_id, doc in current.items():
            if doc_id not in rows:
                stale.append((doc_id, doc))
                continue
            old_rel, old_hash = rows[doc_id]
            if old_hash != fnv1a_hex(doc.body):
                stale.append((doc_id, doc))
            elif old_rel != doc.rel:  # rename, same content — no re-embed
                self._conn.execute(
                    "UPDATE docs SET rel = ?, title = ?, category = ?, type = ? WHERE id = ?;",
                    (
                        doc.rel,
                        str(doc.meta.get("title", "")),
                        str(doc.meta.get("category", "")),
                        str(doc.meta.get("type", "")),
                        doc_id,
                    ),
                )

        if stale:
            vecs = await embed_batch([d.body for _, d in stale])
            for (_, doc), vec in zip(stale, vecs):
                self.upsert(doc.rel, doc.meta, doc.body, vec)
        self._conn.commit()
        return {"embedded": len(stale), "removed": len(removed), "total": len(current)}

    def match(
        self, query_embedding: list[float], *, count: int, min_similarity: float
    ) -> list[dict]:
        """Cosine KNN, then resolve hits back to files — bodies are read fresh
        so a just-edited file is served correctly even before a refresh."""
        q = serialize_float32(list(query_embedding))
        rows = self._conn.execute(
            """
            SELECT d.id, d.rel, d.type, d.title, d.category,
                   vec_distance_cosine(v.embedding, ?) AS dist
            FROM vec_docs v JOIN docs d ON d.id = v.doc_id
            ORDER BY dist ASC LIMIT ?;
            """,
            (q, count),
        ).fetchall()
        out: list[dict] = []
        for doc_id, rel, typ, title, category, dist in rows:
            sim = 1.0 - float(dist)
            if sim < min_similarity:
                continue
            doc = self._bundle.read(rel)
            if doc is None:  # deleted/mangled since last refresh — tolerated
                continue
            out.append(
                {
                    "id": doc_id,
                    "rel": rel,
                    "type": str(doc.meta.get("type", typ)),
                    "title": str(doc.meta.get("title", title)),
                    "category": str(doc.meta.get("category", category)),
                    "content": doc.body.rstrip(),
                    "similarity": sim,
                }
            )
        return out
```

- [ ] **Step 5: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_sidecar.py -v && .venv/bin/pytest tests/ -q && .venv/bin/ruff check .
git add -A && git commit -m "feat: sqlite-vec sidecar index — hash-diff refresh, rename reassociation, file-resolving match

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 3 passed in test_sidecar; full suite green.

---

### Task 7: Writer — the persist boundary with cross-run dedup

**Files:**
- Create: `delapan82/bundle/writer.py`
- Test: `tests/test_writer.py`

**Interfaces:**
- Consumes: `Finding` (Task 2), `blended_confidence` (Task 2), `Bundle` (Task 5), `SidecarIndex.match/upsert` (Task 6), `slugify`/`unique_path` (Task 4), `embed_text` (Task 2), `get_config().dedup.threshold`.
- Produces:
  - `render_content(content: Any) -> str` and `normalize_provenance(provenance: Any) -> list[dict]` — ported verbatim from upstream `mcp/server.py` (`_render_content`/`_normalize_provenance`, the previously-duplicated helpers, now living in exactly one place)
  - `finding_frontmatter(f: Finding) -> dict`
  - `async persist_findings(bundle, index, findings: list[Finding], *, cap: int) -> dict` returning `{"written": [rel...], "merged": [rel...]}`; side effects: files written, index updated (no double-embed), `log.md` appended, `index.md` rebuilt

- [ ] **Step 1: Write the failing tests** — `tests/test_writer.py`

```python
from __future__ import annotations

from delapan82.bundle.frontmatter import parse_doc
from delapan82.bundle.writer import persist_findings, render_content
from delapan82.core.exploration.models import Finding
from delapan82.index.sidecar import SidecarIndex


def _finding(title: str, content: dict, urls: list[str]) -> Finding:
    return Finding(
        exploration_id="e1",
        project_id="p1",
        category="fact",
        title=title,
        content=content,
        confidence=0.4,
        provenance=[{"url": u, "query": "q"} for u in urls],
        tags=["t1"],
        extraction_model="anthropic/claude-sonnet-4.6",
    )


def test_render_content_matches_upstream_shape():
    assert render_content("already a string") == "already a string"
    assert render_content({"summary": "only value"}) == "only value"
    md = render_content({"key_fact": "v", "items": ["a", "b"]})
    assert "**Key Fact**: v" in md and "```json" in md


async def test_persist_writes_conformant_finding_file(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    out = await persist_findings(
        bundle, idx, [_finding("HK adoption", {"summary": "s1"}, ["http://a"])], cap=10
    )
    assert out["written"] == ["findings/hk-adoption.md"] and out["merged"] == []
    meta, body = parse_doc((bundle.root / "findings" / "hk-adoption.md").read_text())
    assert meta["type"] == "finding" and meta["id"] and meta["title"] == "HK adoption"
    assert meta["source_count"] == 1 and meta["provenance"][0]["url"] == "http://a"
    assert "accessed_at" in meta["provenance"][0]
    assert body.rstrip() == "s1"
    # log + index maintained
    assert "hk-adoption.md" in (bundle.root / "log.md").read_text()
    assert "hk-adoption.md" in (bundle.root / "index.md").read_text()


async def test_persist_dedups_across_runs(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    f1 = _finding("HK adoption", {"summary": "same body"}, ["http://a"])
    await persist_findings(bundle, idx, [f1], cap=10)
    # run 2: identical content, new source → merged, not duplicated
    f2 = _finding("HK adoption again", {"summary": "same body"}, ["http://b"])
    out = await persist_findings(bundle, idx, [f2], cap=10)
    assert out["written"] == [] and out["merged"] == ["findings/hk-adoption.md"]
    files = list((bundle.root / "findings").glob("*.md"))
    assert len(files) == 1
    meta, _ = parse_doc(files[0].read_text())
    urls = {p["url"] for p in meta["provenance"]}
    assert urls == {"http://a", "http://b"} and meta["source_count"] == 2
    assert meta["confidence"] == round(1.0 - 0.6**2, 4)  # blended, quality=1.0


async def test_persist_respects_cap(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    fs = [_finding(f"T{i}", {"summary": f"body {i}"}, ["http://x"]) for i in range(5)]
    out = await persist_findings(bundle, idx, fs, cap=2)
    assert len(out["written"]) == 2
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_writer.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan82.bundle.writer'`

- [ ] **Step 3: Write the implementation** — `delapan82/bundle/writer.py`

```python
"""THE persist boundary: pipeline/agent findings → OKF files + index rows.

    Finding ─► render_content ─► embed ─► dedup vs index ─► merge OR new file
                                              │
                       log.md entry + index.md rebuild + sidecar upsert

Cross-run dedup (new vs upstream, which duplicated on re-run): a finding whose
body is ≥ dedup.threshold cosine-similar to an existing `finding` doc merges
into it — provenance union + recomputed confidence; the existing body wins
(v1 merges evidence, not prose)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from delapan82.bundle.core import Bundle
from delapan82.bundle.identity import slugify, unique_path
from delapan82.core.clients.embeddings import embed_text
from delapan82.core.config import get_config
from delapan82.core.exploration.merger import blended_confidence
from delapan82.core.exploration.models import Finding
from delapan82.index.sidecar import SidecarIndex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_content(content: Any) -> str:
    """Render a finding's free-form ``content`` dict to a markdown body.
    Ported verbatim from upstream ``mcp/server.py::_render_content`` — the
    persisted shape stays byte-identical to delapan's."""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return str(content)
    if not content:
        return ""

    if len(content) == 1:
        only = next(iter(content.values()))
        if isinstance(only, str):
            return only

    lines: list[str] = []
    for key, value in content.items():
        label = key.replace("_", " ").title()
        if isinstance(value, (list, dict)):
            lines.append(f"**{label}**:")
            lines.append("```json")
            lines.append(json.dumps(value, indent=2))
            lines.append("```")
        else:
            lines.append(f"**{label}**: {value}")
    return "\n".join(lines)


def normalize_provenance(provenance: Any) -> list[dict]:
    """Findings carry ``[{url, query}]``; keep that shape, stamp ``accessed_at``."""
    if not provenance:
        return []
    out: list[dict] = []
    for p in provenance:
        if isinstance(p, dict):
            entry = dict(p)
            entry.setdefault("accessed_at", _now_iso())
            out.append(entry)
    return out


def finding_frontmatter(f: Finding) -> dict:
    """OKF frontmatter for a pipeline finding — includes the fields upstream's
    persist silently dropped (quality, source_count, exploration_id,
    extraction_model), kept deliberately per spec."""
    return {
        "id": f.id,
        "type": "finding",
        "title": f.title,
        "category": f.category,
        "confidence": round(float(f.confidence), 4),
        "quality": float(f.quality),
        "source_count": int(f.source_count),
        "tags": list(f.tags or []),
        "provenance": normalize_provenance(f.provenance),
        "exploration_id": f.exploration_id,
        "extraction_model": f.extraction_model,
        "created_at": f.created_at.isoformat(),
        "timestamp": _now_iso(),
    }


def _union_provenance(existing: list, incoming: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for p in [*(existing or []), *incoming]:
        if isinstance(p, dict) and p.get("url"):
            seen.setdefault(str(p["url"]), p)
    return list(seen.values())


async def persist_findings(
    bundle: Bundle, index: SidecarIndex, findings: list[Finding], *, cap: int
) -> dict:
    """Persist up to `cap` findings as OKF files; dedup against the index."""
    threshold = get_config().dedup.threshold
    written: list[str] = []
    merged: list[str] = []
    log_entries: list[str] = []

    for f in findings[:cap]:
        body = render_content(f.content)
        vec = await embed_text(body)
        hits = [
            h
            for h in index.match(vec, count=3, min_similarity=0.0)
            if h["type"] == "finding"
        ]
        top = hits[0] if hits else None

        if top and top["similarity"] >= threshold:
            doc = bundle.read(top["rel"])
            if doc is not None:
                meta = doc.meta
                prov = _union_provenance(
                    meta.get("provenance") or [], normalize_provenance(f.provenance)
                )
                meta["provenance"] = prov
                meta["source_count"] = len({p.get("url") for p in prov if p.get("url")}) or 1
                meta["confidence"] = round(
                    blended_confidence(meta["source_count"], float(meta.get("quality", 1.0))), 4
                )
                meta["timestamp"] = _now_iso()
                bundle.write(top["rel"], meta, doc.body)
                index.upsert(top["rel"], meta, doc.body, vec)
                merged.append(top["rel"])
                log_entries.append(
                    f"**Update**: merged new evidence into [{meta.get('title', '')}]({top['rel']})"
                )
                continue

        meta = finding_frontmatter(f)
        path = unique_path(bundle.root / "findings", slugify(f.title))
        rel = f"findings/{path.name}"
        bundle.write(rel, meta, body)
        index.upsert(rel, meta, body, vec)
        written.append(rel)
        log_entries.append(f"**Creation**: [{f.title}]({rel})")

    bundle.rebuild_index_md()
    bundle.append_log(log_entries, day=datetime.now(timezone.utc).date().isoformat())
    return {"written": written, "merged": merged}
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_writer.py -v && .venv/bin/pytest tests/ -q && .venv/bin/ruff check .
git add -A && git commit -m "feat: persist boundary — findings to OKF files with cross-run dedup

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 4 passed in test_writer; full suite green.

---

### Task 8: Synopsis — file-backed spine

**Files:**
- Create: `delapan82/core/agent/synopsis.py`
- Test: `tests/test_synopsis.py`

**Interfaces:**
- Consumes: `Bundle` (Task 5), `chat_model` (Task 2), `SynopsisConfig`/`get_config` (Task 1), `should_rebuild` logic verbatim from upstream.
- Produces: `SYNOPSIS_REL = "synopsis.md"`; `should_rebuild(live_count: int, row: dict | None, cfg: SynopsisConfig) -> bool` (verbatim port); `load_synopsis(bundle) -> dict | None` returning `{"entries": [{topic, gloss}], "finding_count_at_build": int, "built_at": str | None, "model": str | None}`; `async maybe_rebuild_synopsis(bundle) -> None` (never raises).

- [ ] **Step 1: Write the failing tests** — `tests/test_synopsis.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import delapan82.core.agent.synopsis as syn
from delapan82.core.agent.synopsis import load_synopsis, maybe_rebuild_synopsis, should_rebuild
from delapan82.core.config import SynopsisConfig


def test_should_rebuild_gates():
    cfg = SynopsisConfig()
    assert should_rebuild(0, None, cfg) is False          # empty KB: never
    assert should_rebuild(1, None, cfg) is True           # no synopsis yet
    row = {"finding_count_at_build": 10, "built_at": datetime.now(timezone.utc).isoformat()}
    assert should_rebuild(10, row, cfg) is False          # fresh, no delta
    assert should_rebuild(25, row, cfg) is True           # +15 findings
    old = {
        "finding_count_at_build": 10,
        "built_at": (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat(),
    }
    assert should_rebuild(10, old, cfg) is True           # > 168h old


async def test_rebuild_writes_okf_synopsis_file(bundle, monkeypatch):
    bundle.write("findings/a.md", {"id": "a1", "type": "finding", "title": "A"}, "body")

    async def fake_build(findings, cfg):
        return [{"topic": "Topic A", "gloss": "what we know"}]

    monkeypatch.setattr(syn, "_build", fake_build)
    await maybe_rebuild_synopsis(bundle)
    row = load_synopsis(bundle)
    assert row["entries"] == [{"topic": "Topic A", "gloss": "what we know"}]
    assert row["finding_count_at_build"] == 1
    text = (bundle.root / "synopsis.md").read_text()
    assert "type: synopsis" in text and "**Topic A**" in text


async def test_rebuild_never_raises(bundle, monkeypatch):
    bundle.write("findings/a.md", {"id": "a1", "type": "finding", "title": "A"}, "body")

    async def boom(findings, cfg):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(syn, "_build", boom)
    await maybe_rebuild_synopsis(bundle)  # must not raise
    assert load_synopsis(bundle) is None
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_synopsis.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation** — `delapan82/core/agent/synopsis.py`

Port of upstream `delapan/core/agent/synopsis.py`: `should_rebuild`, `_build_prompt`, `_build` are copied verbatim (imports renamed); storage swaps the Store row for `synopsis.md`. Full file:

```python
"""KB synopsis spine: the always-on preamble's stable layer, as an OKF file.

    finding docs (titles+categories) ─► fast LLM ─► entries ─► synopsis.md

Regen is incremental and best-effort: `maybe_rebuild_synopsis` never raises.
Entries live in frontmatter (`entries:`) so consumers read them without
parsing prose; the body renders the same list for humans."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from delapan82.bundle.core import Bundle
from delapan82.core.clients.anthropic import chat_model
from delapan82.core.config import SynopsisConfig, get_config

logger = logging.getLogger(__name__)

SYNOPSIS_REL = "synopsis.md"


def should_rebuild(live_count: int, row: dict | None, cfg: SynopsisConfig) -> bool:
    if live_count <= 0:
        return False
    if row is None:
        return True
    if live_count - int(row.get("finding_count_at_build", 0)) >= cfg.rebuild_delta:
        return True
    built_at = row.get("built_at")
    if built_at:
        ts = datetime.fromisoformat(str(built_at).replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        if age_h >= cfg.rebuild_max_age_hours:
            return True
    return False


def _build_prompt(findings: list[dict], cfg: SynopsisConfig) -> str:
    lines = [f"- {f.get('title', '')} [{f.get('category', '')}]" for f in findings]
    catalogue = "\n".join(lines)
    return (
        "You are summarizing a knowledge base into a compact orientation spine.\n"
        f"Below are its findings (title [category]).\n\n{catalogue}\n\n"
        f"Produce at most {cfg.max_entries} entries naming the KB's main topics. "
        "Return ONLY JSON: a list of objects with keys `topic` (short noun phrase) "
        "and `gloss` (one sentence on what the KB knows about it)."
    )


async def _build(findings: list[dict], cfg: SynopsisConfig) -> list[dict]:
    llm = chat_model(cfg.model)
    resp = await llm.ainvoke([{"role": "user", "content": _build_prompt(findings, cfg)}])
    text = resp.content if isinstance(resp.content, str) else ""
    try:
        data = json.loads(text[text.find("[") : text.rfind("]") + 1])
        # Load-bearing: the dict-filter keeps the [:max_entries] slice safe.
        return [e for e in data if isinstance(e, dict)][: cfg.max_entries]
    except (ValueError, json.JSONDecodeError):
        logger.warning("synopsis JSON parse failed; len=%d", len(text))
        return []


def load_synopsis(bundle: Bundle) -> dict | None:
    """Current synopsis, or None. Shape mirrors the upstream row so
    `should_rebuild` ports unchanged."""
    doc = bundle.read(SYNOPSIS_REL)
    if doc is None:
        return None
    return {
        "entries": doc.meta.get("entries") or [],
        "finding_count_at_build": int(doc.meta.get("finding_count_at_build", 0)),
        "built_at": doc.meta.get("built_at"),
        "model": doc.meta.get("model"),
    }


async def maybe_rebuild_synopsis(bundle: Bundle) -> None:
    """Best-effort: rebuild synopsis.md if the KB grew enough. Never raises."""
    try:
        cfg = get_config().synopsis
        finding_docs = list(bundle.docs(type="finding"))
        live_count = len(finding_docs)
        row = load_synopsis(bundle)
        if not should_rebuild(live_count, row, cfg):
            return
        listing = [
            {"title": d.meta.get("title", ""), "category": d.meta.get("category", "")}
            for d in finding_docs[:200]
        ]
        entries = await _build(listing, cfg)
        now = datetime.now(timezone.utc).isoformat()
        body = "\n".join(f"- **{e.get('topic', '')}**: {e.get('gloss', '')}" for e in entries)
        meta = {
            "type": "synopsis",
            "entries": entries,
            "finding_count_at_build": live_count,
            "built_at": now,
            "model": cfg.model,
            "timestamp": now,
        }
        bundle.write(SYNOPSIS_REL, meta, body)
    except Exception:  # noqa: BLE001 — regen is best-effort, never breaks a turn
        logger.exception("synopsis rebuild failed for bundle=%s", bundle.root)
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_synopsis.py -v && .venv/bin/ruff check .
git add -A && git commit -m "feat: file-backed synopsis spine (synopsis.md, upstream rebuild gates)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 3 passed.

---

### Task 9: Preamble — banding verbatim, markdown render, graceful degrade

**Files:**
- Create: `delapan82/core/agent/preamble.py`
- Test: `tests/test_preamble.py`

**Interfaces:**
- Consumes: `TiersConfig`/`get_config` (Task 1), `embed_text` (Task 2), `SidecarIndex.match` (Task 6), `load_synopsis` (Task 8).
- Produces: `Coverage = Literal["rich", "sparse", "gap", "unknown"]`; `Depth = Literal["shallow", "normal", "deep"]`; `band_findings(rows, cfg) -> dict[int, list[dict]]` and `assess_coverage(bands, cfg) -> Coverage` (verbatim ports); `render_preamble(synopsis: list[dict], bands, *, depth, cfg) -> str` (markdown OKF-excerpt stream, hard `preamble_char_budget` ceiling, `excerpt_char_cap` per doc); `async select_preamble(query, *, bundle, index, depth="normal") -> tuple[str, Coverage]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_preamble.py`

```python
from __future__ import annotations

from delapan82.core.agent.preamble import (
    assess_coverage,
    band_findings,
    render_preamble,
    select_preamble,
)
from delapan82.core.config import TiersConfig
from delapan82.index.sidecar import SidecarIndex


def _row(sim: float, title: str = "T", body: str = "b") -> dict:
    return {"id": "x", "rel": "findings/t.md", "type": "finding", "title": title,
            "category": "fact", "content": body, "similarity": sim}


def test_banding_thresholds():
    cfg = TiersConfig()
    bands = band_findings([_row(0.6), _row(0.5), _row(0.3), _row(0.1)], cfg)
    assert [len(bands[b]) for b in (1, 2, 3)] == [1, 1, 1]  # 0.1 dropped


def test_coverage_verdicts():
    cfg = TiersConfig()
    assert assess_coverage({1: [_row(0.6)] * 3, 2: [], 3: []}, cfg) == "rich"
    assert assess_coverage({1: [], 2: [_row(0.45)], 3: []}, cfg) == "sparse"
    assert assess_coverage({1: [], 2: [], 3: []}, cfg) == "gap"


def test_render_budget_is_hard_ceiling():
    cfg = TiersConfig(preamble_char_budget=600, excerpt_char_cap=1200)
    rows = [_row(0.9 - i * 0.01, title=f"T{i}", body="x" * 400) for i in range(10)]
    out = render_preamble([], {1: rows, 2: [], 3: []}, depth="normal", cfg=cfg)
    assert len(out) <= 600
    assert out.count("title:") >= 1  # at least one excerpt admitted


def test_render_includes_synopsis_and_source_paths():
    cfg = TiersConfig()
    out = render_preamble(
        [{"topic": "A", "gloss": "g"}], {1: [_row(0.7)], 2: [], 3: []}, depth="normal", cfg=cfg
    )
    assert "## Synopsis" in out and "- **A**: g" in out
    assert "source: findings/t.md" in out


async def test_select_preamble_degrades_to_unknown_without_embeddings(
    bundle, monkeypatch
):
    async def boom(text):
        raise RuntimeError("no key")

    import delapan82.core.agent.preamble as pre

    monkeypatch.setattr(pre, "embed_text", boom)
    idx = SidecarIndex(bundle)
    text, coverage = await select_preamble("q", bundle=bundle, index=idx, depth="normal")
    assert coverage == "unknown"


async def test_select_preamble_end_to_end(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    bundle.write(
        "findings/a.md",
        {"id": "a1", "type": "finding", "title": "Alpha", "category": "fact"},
        "alpha body",
    )
    await idx.refresh()
    text, coverage = await select_preamble(
        "alpha body", bundle=bundle, index=idx, depth="normal"
    )
    assert coverage in {"rich", "sparse"} and "alpha body" in text
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_preamble.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation** — `delapan82/core/agent/preamble.py`

```python
"""Always-on KB preamble: synopsis spine + dynamic similarity bands.

    embed(query) ─► index.match ─► band_findings ─► assess_coverage
                            │
        load_synopsis ─────┴─► render_preamble ─► markdown excerpt stream

The pure core (band_findings / assess_coverage / render_preamble) has no IO
and ports verbatim from delapan — only the render target changed: the bespoke
XML dialect is replaced by a stream of OKF excerpts (frontmatter summary +
body) whose `source:` lines point back into the bundle so the agent can keep
reading with its own file tools."""

from __future__ import annotations

from typing import Literal

from delapan82.bundle.core import Bundle
from delapan82.core.agent.synopsis import load_synopsis
from delapan82.core.clients.embeddings import embed_text
from delapan82.core.config import TiersConfig, get_config
from delapan82.index.sidecar import SidecarIndex

Coverage = Literal["rich", "sparse", "gap", "unknown"]
Depth = Literal["shallow", "normal", "deep"]

_DEPTH_BANDS: dict[str, tuple[int, ...]] = {
    "shallow": (1,),
    "normal": (1, 2),
    "deep": (1, 2, 3),
}


def band_findings(rows: list[dict], cfg: TiersConfig) -> dict[int, list[dict]]:
    """Bucket match rows by `similarity` into bands 1/2/3; below band3_min drops."""
    bands: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for r in sorted(rows, key=lambda r: r.get("similarity", 0.0), reverse=True):
        s = r.get("similarity", 0.0)
        if s >= cfg.band1_min:
            bands[1].append(r)
        elif s >= cfg.band2_min:
            bands[2].append(r)
        elif s >= cfg.band3_min:
            bands[3].append(r)
    return bands


def assess_coverage(bands: dict[int, list[dict]], cfg: TiersConfig) -> Coverage:
    """rich = enough band-1 hits; gap = nothing banded; else sparse."""
    if len(bands[1]) >= cfg.rich_hit_count:
        return "rich"
    if not (bands[1] or bands[2] or bands[3]):
        return "gap"
    return "sparse"


def _render_excerpt(r: dict, cap: int) -> str:
    body = (r.get("content") or "")[:cap]
    return (
        "\n---\n"
        f"title: {r.get('title', '')}\n"
        f"category: {r.get('category', '')}\n"
        f"source: {r.get('rel', '')}\n"
        "---\n"
        f"{body}"
    )


def render_preamble(
    synopsis: list[dict],
    bands: dict[int, list[dict]],
    *,
    depth: Depth = "normal",
    cfg: TiersConfig | None = None,
) -> str:
    """Assemble the preamble markdown under a hard char ceiling —
    lowest-similarity excerpts drop first, same policy as upstream."""
    cfg = cfg or get_config().tiers
    parts: list[str] = []

    if synopsis:
        parts.append("## Synopsis")
        parts.extend(f"- **{e.get('topic', '')}**: {e.get('gloss', '')}" for e in synopsis)

    selected: list[dict] = []
    for b in _DEPTH_BANDS.get(depth, _DEPTH_BANDS["normal"]):
        selected.extend(bands.get(b, []))
    selected.sort(key=lambda r: r.get("similarity", 0.0), reverse=True)

    header = "\n## Findings"
    used = sum(len(p) + 1 for p in parts)
    rendered: list[str] = []
    for r in selected:
        block = _render_excerpt(r, cfg.excerpt_char_cap)
        added = len(block) + 1 + (len(header) + 1 if not rendered else 0)
        if used + added > cfg.preamble_char_budget:
            break
        rendered.append(block)
        used += added

    if rendered:
        parts.append(header)
        parts.extend(rendered)
    if not parts:
        parts.append("_(empty bundle — no synopsis, no matches)_")
    return "\n".join(parts)


async def select_preamble(
    query: str | None,
    *,
    bundle: Bundle,
    index: SidecarIndex,
    depth: Depth = "normal",
) -> tuple[str, Coverage]:
    """IO entry: synopsis + (optional) banded matches → (markdown, coverage).
    No embedding provider → coverage 'unknown', synopsis-only preamble —
    reading files never blocks on a key."""
    cfg = get_config().tiers
    syn = load_synopsis(bundle)
    entries = (syn or {}).get("entries") or []

    bands: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    coverage: Coverage = "gap"
    if query:
        try:
            qvec = await embed_text(query)
        except Exception:  # noqa: BLE001 — keyless/degraded: serve files, flag unknown
            return render_preamble(entries, bands, depth=depth, cfg=cfg), "unknown"
        rows = index.match(
            qvec, count=get_config().search.max_limit, min_similarity=cfg.band3_min
        )
        bands = band_findings(rows, cfg)
        coverage = assess_coverage(bands, cfg)

    return render_preamble(entries, bands, depth=depth, cfg=cfg), coverage
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_preamble.py -v && .venv/bin/pytest tests/ -q && .venv/bin/ruff check .
git add -A && git commit -m "feat: preamble — banding/coverage ported verbatim, OKF markdown render, unknown-coverage degrade

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 6 passed in test_preamble; full suite green.

---

### Task 10: MCP server — resume / search / explore

**Files:**
- Create: `delapan82/mcp/__init__.py` (empty), `delapan82/mcp/banner.py`, `delapan82/mcp/server.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: everything above. Tools take **no project/kb args** — `Bundle.discover()` from CWD; branch/repo names only feed `Finding` provenance fields.
- Produces: module runnable as `python -m delapan82.mcp.server`; tools:
  - `delapan_resume(query: str | None = None, depth: Depth = "normal") -> dict` → `{"banner", "coverage", "bundle_path", "preamble"}` or `{"error"}`
  - `delapan_search(query: str, limit: int | None = None) -> dict` → `{"query", "hits": [{"path", "title", "type", "category", "excerpt", "similarity"}]}` or `{"error"}`
  - `delapan_explore(prompt: str, max_findings: int | None = None) -> dict` → `{"written", "merged", "count", "bundle_path"}`

- [ ] **Step 1: Write `delapan82/mcp/banner.py`**

```python
"""delapan-82 wordmark — leads the resume surface (conversation concern only)."""

from __future__ import annotations

BANNER = """\
        ╱──
──────◌
        ╲──
   d e l a p a n · 8 2
   knowledge, in files"""
```

- [ ] **Step 2: Write the failing tests** — `tests/test_mcp_tools.py`

Tools are tested through their underlying implementations with a patched CWD (FastMCP's `@mcp.tool()` leaves the wrapped function callable).

```python
from __future__ import annotations

import delapan82.mcp.server as srv
from delapan82.index.sidecar import SidecarIndex


async def test_resume_reports_missing_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .git, no knowledge/
    out = await srv.delapan_resume()
    assert "error" in out


async def test_resume_and_search_over_seeded_bundle(tmp_path, monkeypatch, fake_embeddings):
    from delapan82.bundle.core import Bundle

    (tmp_path / ".git").mkdir()
    bundle = Bundle.init(tmp_path / "knowledge")
    bundle.write(
        "findings/a.md",
        {"id": "a1", "type": "finding", "title": "Alpha", "category": "fact"},
        "alpha body",
    )
    await SidecarIndex(bundle).refresh()
    monkeypatch.chdir(tmp_path)

    out = await srv.delapan_resume(query="alpha body")
    assert out["coverage"] in {"rich", "sparse"}
    assert out["bundle_path"].endswith("knowledge")
    assert "alpha body" in out["preamble"] and out["banner"]

    hits = await srv.delapan_search(query="alpha body")
    assert hits["hits"][0]["path"] == "findings/a.md"
    assert hits["hits"][0]["similarity"] > 0.99


async def test_search_reports_missing_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = await srv.delapan_search(query="x")
    assert "error" in out
```

- [ ] **Step 3: Run to verify failure**

```bash
.venv/bin/pytest tests/test_mcp_tools.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'delapan82.mcp.server'`

- [ ] **Step 4: Write the implementation** — `delapan82/mcp/server.py`

```python
"""FastMCP stdio server — the plugin's entry path into delapan-82.

    MCP tool call ─► Bundle.discover(CWD) ─► SidecarIndex ─► engine

Three tools (project/kb args are gone — the repo IS the project, the branch
IS the kb, the bundle rides the checkout):

    delapan_resume   — coverage + bundle pointer + rendered OKF preamble
    delapan_search   — semantic recall; hits are file pointers, not row dumps
    delapan_explore  — research pipeline → OKF files (with cross-run dedup)

Run with: ``python -m delapan82.mcp.server``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from delapan82.bundle.core import Bundle
from delapan82.bundle.writer import persist_findings
from delapan82.core.agent.preamble import Depth, select_preamble
from delapan82.core.agent.synopsis import maybe_rebuild_synopsis
from delapan82.core.clients.embeddings import embed_text
from delapan82.core.config import get_config, get_settings
from delapan82.core.exploration import run_exploration
from delapan82.index.sidecar import SidecarIndex

from .banner import BANNER

logger = logging.getLogger(__name__)

mcp = FastMCP("delapan82")

_NO_BUNDLE = (
    "no OKF bundle found — run delapan_explore to create one, or create "
    "<repo>/{dir}/ by hand (see the capture skill)"
)


def _repo_root() -> Path:
    cur = Path.cwd().resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return cur


def _git_branch(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "default"
    except Exception:  # noqa: BLE001 — branch name is provenance garnish only
        return "default"


async def _refreshed_index(bundle: Bundle) -> SidecarIndex:
    index = SidecarIndex(bundle)
    try:
        await index.refresh()
    except Exception:  # noqa: BLE001 — keyless refresh fails on embed; serve stale index
        logger.warning("index refresh failed (no embedding provider?); serving stale index")
    return index


# --- Inject → this conversation --------------------------------------------


@mcp.tool()
async def delapan_resume(query: str | None = None, depth: Depth = "normal") -> dict:
    """Inject KB context into THIS conversation. Returns ``{"banner",
    "coverage", "bundle_path", "preamble"}`` — a curated markdown render of
    the bundle plus the directory pointer; continue with Read/Grep on
    ``bundle_path`` for anything deeper."""
    bundle = Bundle.discover()
    if bundle is None:
        return {"error": _NO_BUNDLE.format(dir=get_config().bundle.dir)}
    index = await _refreshed_index(bundle)
    preamble, coverage = await select_preamble(query, bundle=bundle, index=index, depth=depth)
    return {
        "banner": BANNER,
        "coverage": coverage,
        "bundle_path": str(bundle.root),
        "preamble": preamble,
    }


# --- Recall ----------------------------------------------------------------


@mcp.tool()
async def delapan_search(query: str, limit: int | None = None) -> dict:
    """Semantic recall over the bundle. Hits are pointers into files
    (path + excerpt + similarity) — Read the path for the full document."""
    bundle = Bundle.discover()
    if bundle is None:
        return {"error": _NO_BUNDLE.format(dir=get_config().bundle.dir)}
    index = await _refreshed_index(bundle)
    try:
        emb = await embed_text(query)
    except Exception as exc:  # noqa: BLE001 — no provider: search needs embeddings
        return {"error": f"semantic search unavailable (no embedding provider): {exc}"}
    hits = index.match(
        emb, count=limit or get_config().search.default_limit, min_similarity=0.0
    )
    return {
        "query": query,
        "hits": [
            {
                "path": h["rel"],
                "title": h["title"],
                "type": h["type"],
                "category": h["category"],
                "excerpt": h["content"][:500],
                "similarity": round(h["similarity"], 4),
            }
            for h in hits
        ],
    }


# --- Build the KB ----------------------------------------------------------


@mcp.tool()
async def delapan_explore(prompt: str, max_findings: int | None = None) -> dict:
    """Run the research pipeline (plan→search→crawl→extract→merge) and persist
    findings as OKF files in the bundle (created on demand). Blocks until
    complete (may take minutes). Returns ``{"written", "merged", "count",
    "bundle_path"}`` — bundle-relative paths of new and dedup-merged docs."""
    root = _repo_root()
    bundle = Bundle.discover() or Bundle.init(root / get_config().bundle.dir)
    index = await _refreshed_index(bundle)
    cfg = get_config().exploration
    cap = min(max_findings or cfg.default_max_findings, cfg.max_findings)

    findings = await run_exploration(
        prompt,
        exploration_id=uuid4().hex,
        project_id=root.name,
        kb_id=_git_branch(root),
        cfg=cfg,
    )
    result = await persist_findings(bundle, index, findings, cap=cap)
    await maybe_rebuild_synopsis(bundle)
    return {
        "written": result["written"],
        "merged": result["merged"],
        "count": len(result["written"]) + len(result["merged"]),
        "bundle_path": str(bundle.root),
    }


def main() -> None:
    get_settings()  # fail fast if the .env is malformed
    mcp.run()


if __name__ == "__main__":
    main()
```

Gotcha: if `@mcp.tool()` wraps functions into non-callable tool objects in the installed `mcp` version, import the underlying functions in the test via `srv.delapan_resume.fn` — check `FastMCP`'s decorator; with `mcp>=1.16` the name remains directly awaitable. Adjust the test imports accordingly, not the server.

- [ ] **Step 5: Run tests, lint, commit**

```bash
.venv/bin/pytest tests/test_mcp_tools.py -v && .venv/bin/pytest tests/ -q && .venv/bin/ruff check .
git add -A && git commit -m "feat: MCP server — resume/search/explore over the OKF bundle (no project/kb args)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: 3 passed in test_mcp_tools; full suite green.

---

### Task 11: Plugin shell — mcp.json, plugin manifest, skills

**Files:**
- Create: `$ROOT/mcp.json`, `$ROOT/.claude-plugin/plugin.json`, `$ROOT/.claude-plugin/marketplace.json`, `$ROOT/skills/resume/SKILL.md`, `$ROOT/skills/search/SKILL.md`, `$ROOT/skills/explore/SKILL.md`, `$ROOT/skills/capture/SKILL.md`

**Interfaces:**
- Consumes: the Task 10 server (`python -m delapan82.mcp.server`).
- Produces: an installable Claude Code plugin `delapan82` with skills `/delapan82:resume`, `/delapan82:search`, `/delapan82:explore`, `/delapan82:capture`.

- [ ] **Step 1: Write `mcp.json`**

```json
{
  "mcpServers": {
    "delapan82": {
      "type": "stdio",
      "command": "/Users/anthonysuherli/Repositories/8star/delapan-82/.venv/bin/python",
      "args": ["-m", "delapan82.mcp.server"]
    }
  }
}
```

(No `cwd` pin — the server must inherit the session CWD, that's how `Bundle.discover()` finds the right repo. This deliberately fixes upstream's hardcoded-cwd gotcha.)

- [ ] **Step 2: Write `.claude-plugin/plugin.json`**

```json
{
  "name": "delapan82",
  "description": "OKF-native knowledge engine — your repo's knowledge/ directory IS the KB. Skills: /delapan82:resume (coverage + preamble + bundle pointer), /delapan82:search (semantic recall → file pointers), /delapan82:explore (web research → OKF files), /delapan82:capture (author findings by hand). Backed by the delapan82 MCP server.",
  "version": "0.1.0",
  "author": { "name": "Anthony Suherli", "email": "anthonysuherli@gmail.com" },
  "license": "AGPL-3.0-or-later",
  "keywords": ["okf", "knowledge-base", "grounding", "findings", "markdown", "mcp"],
  "skills": ["./skills/resume", "./skills/search", "./skills/explore", "./skills/capture"],
  "mcpServers": "./mcp.json"
}
```

- [ ] **Step 3: Write `.claude-plugin/marketplace.json`**

```json
{
  "name": "delapan82",
  "owner": { "name": "Anthony Suherli", "email": "anthonysuherli@gmail.com" },
  "metadata": {
    "description": "Local dev marketplace for the delapan82 plugin — OKF-native knowledge engine.",
    "version": "0.1.0"
  },
  "plugins": [
    {
      "name": "delapan82",
      "source": "./",
      "description": "OKF-native knowledge engine: knowledge/ in your repo is the KB. /delapan82:resume, /delapan82:search, /delapan82:explore, /delapan82:capture.",
      "version": "0.1.0",
      "strict": false
    }
  ]
}
```

- [ ] **Step 4: Write the four SKILL.md files**

`skills/resume/SKILL.md`:
````markdown
---
name: delapan82-resume
description: Tap the repo's OKF bundle and return a preamble + coverage + bundle pointer. Use before answering repo-specific questions or when the user asks where they left off.
---

# delapan-82 Resume

Ground repo work in the bundle before answering. No target resolution needed —
the server discovers `knowledge/` from the working directory (the branch you
have checked out IS the KB).

## Workflow

1. Call the **`delapan_resume`** MCP tool (optional `query` = the user's topic).
2. Read `coverage` (`rich` / `sparse` / `gap` / `unknown`) and the markdown `preamble`.
3. `bundle_path` is a real directory: for anything deeper than the preamble,
   Read/Grep the files directly — excerpts carry `source:` paths for this.
4. If coverage is `gap`, offer `/delapan82:explore`. If `unknown`, embeddings are
   unavailable (no key) — the bundle is still fully readable as files.
5. Treat the preamble as authoritative context for this repo/branch.
````

`skills/search/SKILL.md`:
````markdown
---
name: delapan82-search
description: Semantic recall over the repo's OKF bundle. Hits are file pointers (path + excerpt + similarity), not row dumps. Use when a question should be answered from captured knowledge.
---

# delapan-82 Search

1. Call **`delapan_search`** with `query` (and optional `limit`).
2. Each hit has `path`, `title`, `excerpt`, `similarity`. For the full document,
   Read `<bundle>/<path>` — the excerpt is a teaser, the file is the truth.
3. If the tool errors with "no embedding provider", fall back to Grep over the
   bundle directory.
````

`skills/explore/SKILL.md`:
````markdown
---
name: delapan82-explore
description: Run the research pipeline (plan → search → crawl → extract → merge) and persist findings as OKF files in knowledge/. Blocks ~1-3 minutes. Requires an LLM key and TAVILY_API_KEY in the delapan-82 .env.
---

# delapan-82 Explore

1. Confirm explore is appropriate — coverage `gap`/`sparse`, or the user asked to research.
2. Call **`delapan_explore`** with `prompt` (the topic) and optional `max_findings`.
3. The result lists `written` (new files) and `merged` (dedup-merged into existing
   files) — bundle-relative paths. New knowledge is ordinary files on the branch:
   review with `git diff`, commit it like code.
4. Re-run **`delapan_resume`** to refresh coverage, then answer from the updated bundle.

## Prerequisites

- `AI_GATEWAY_API_KEY` (or `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) and `TAVILY_API_KEY`
  in `/Users/anthonysuherli/Repositories/8star/delapan-82/.env`.
  If keys are missing, tell the user which env vars to set — do not silently skip.
````

`skills/capture/SKILL.md`:
````markdown
---
name: delapan82-capture
description: Author a finding or concept directly into the repo's OKF bundle with the Write tool. Use to persist session insights, decisions, or facts worth keeping — the engine adopts the file on its next read (embeds + indexes it automatically).
---

# delapan-82 Capture

The Write tool IS the ingestion API. Write a markdown file into the bundle and
the sidecar index adopts it on the next `delapan_resume`/`delapan_search` call.

## Finding template

Write to `<bundle>/findings/<kebab-case-slug>.md`:

```markdown
---
id: <8 random hex chars, e.g. 3f9a2c1e>
type: finding
title: <concise, specific title>
category: <topic category>
confidence: 0.4
quality: 1.0
source_count: 1
tags: []
provenance:
  - url: <source url or repo path>
    accessed_at: <ISO 8601 now>
created_at: <ISO 8601 now>
timestamp: <ISO 8601 now>
---

<markdown body: the fact/insight, favor **Key**: value lines and lists>
```

## Concept template

Write to `<bundle>/concepts/<slug>.md` with `type: concept`, `title`, and an
optional `# Evidence` section linking findings, e.g. `[title](/findings/x.md)`.

## Rules

- `type` is mandatory (OKF conformance); everything else degrades gracefully.
- Never edit `.index/` — it's derived and gitignored.
- Knowledge is code: show the user the diff, commit on the branch.
````

- [ ] **Step 5: Validate JSON, commit**

```bash
cd $ROOT && for f in mcp.json .claude-plugin/plugin.json .claude-plugin/marketplace.json; do .venv/bin/python -c "import json;json.load(open('$f'))" || exit 1; done && echo JSON-OK
git add -A && git commit -m "feat: plugin shell — mcp.json, manifests, resume/search/explore/capture skills

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: `JSON-OK`.

---

### Task 12: Conformance suite, integration chain, README

**Files:**
- Create: `tests/test_conformance.py`, `tests/test_integration_chain.py`, `$ROOT/README.md`

**Interfaces:**
- Consumes: everything.
- Produces: `assert_okf_conformant(root: Path)` test helper (inside test_conformance.py); the v1 success-criteria chain verified end-to-end with fakes.

- [ ] **Step 1: Write `tests/test_conformance.py`**

```python
from __future__ import annotations

from pathlib import Path

from delapan82.bundle.frontmatter import parse_doc
from delapan82.bundle.writer import persist_findings
from delapan82.core.exploration.models import Finding
from delapan82.index.sidecar import SidecarIndex


def assert_okf_conformant(root: Path) -> None:
    """OKF v0.1 producer conformance: every non-reserved .md parses with a
    non-empty `type`; reserved files carry no frontmatter (root index.md may
    carry only okf_version)."""
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".index/"):
            continue
        text = path.read_text(encoding="utf-8")
        if path.name == "log.md":
            assert not text.startswith("---"), f"{rel}: log.md must not have frontmatter"
        elif path.name == "index.md":
            if rel == "index.md" and text.startswith("---"):
                meta, _ = parse_doc(text)
                assert set(meta) == {"okf_version"}, f"{rel}: only okf_version allowed"
        else:
            parsed = parse_doc(text)
            assert parsed is not None, f"{rel}: unparseable frontmatter"
            assert str(parsed[0].get("type", "")).strip(), f"{rel}: empty type"


async def test_engine_written_bundle_is_conformant(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)
    fs = [
        Finding(
            exploration_id="e1", project_id="p", category="fact",
            title=f"Fact {i}", content={"summary": f"body {i}"},
            provenance=[{"url": f"http://s{i}"}],
        )
        for i in range(3)
    ]
    await persist_findings(bundle, idx, fs, cap=10)
    assert_okf_conformant(bundle.root)


def test_consumer_tolerates_unknown_types_and_broken_links(bundle):
    bundle.write(
        "notes/weird.md",
        {"id": "w1", "type": "totally-novel-type", "x_future": [1, 2]},
        "See [gone](/findings/does-not-exist.md).",
    )
    docs = list(bundle.docs())
    assert any(d.meta["type"] == "totally-novel-type" for d in docs)  # not rejected
```

- [ ] **Step 2: Write `tests/test_integration_chain.py`** — the v1 success-criteria chain (spec §success criteria 2, 3, 4) with fakes

```python
from __future__ import annotations

from delapan82.core.agent.preamble import select_preamble
from delapan82.bundle.writer import persist_findings
from delapan82.core.exploration.models import Finding
from delapan82.index.sidecar import SidecarIndex


async def test_persist_resume_search_edit_adopt_chain(bundle, fake_embeddings):
    idx = SidecarIndex(bundle)

    # 1. pipeline persists a finding
    f = Finding(
        exploration_id="e1", project_id="p", category="fact",
        title="Solvency basis", content={"summary": "prescribed valuation basis"},
        provenance=[{"url": "http://a"}],
    )
    await persist_findings(bundle, idx, [f], cap=10)

    # 2. fresh-session resume sees it (criterion 2)
    text, coverage = await select_preamble(
        "prescribed valuation basis", bundle=bundle, index=idx
    )
    assert coverage in {"rich", "sparse"} and "prescribed valuation basis" in text

    # 3. human edit reflected on next read, no manual step (criterion 3)
    path = bundle.root / "findings" / "solvency-basis.md"
    path.write_text(
        path.read_text().replace("prescribed valuation basis", "REVISED basis text")
    )
    await idx.refresh()
    text, _ = await select_preamble("REVISED basis text", bundle=bundle, index=idx)
    assert "REVISED basis text" in text

    # 4. agent-authored file adopted on next read (criterion 4)
    bundle.write(
        "findings/manual-note.md",
        {"id": "m1", "type": "finding", "title": "Manual note", "category": "note"},
        "hand-written insight",
    )
    await idx.refresh()
    text, _ = await select_preamble("hand-written insight", bundle=bundle, index=idx)
    assert "hand-written insight" in text
```

- [ ] **Step 3: Run the new tests**

```bash
.venv/bin/pytest tests/test_conformance.py tests/test_integration_chain.py -v
```
Expected: 3 passed.

- [ ] **Step 4: Write `$ROOT/README.md`**

```markdown
# delapan-82

OKF-native knowledge engine — a hard fork of [delapan](https://delapan.ai) where
the knowledge base is an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle (`knowledge/`) living inside the repo it describes. Files are canonical;
git is the sync layer; a gitignored `.index/` sqlite-vec sidecar is derived and
rebuilt from files on demand.

## Quick start

    python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
    cp .env.example .env   # add AI_GATEWAY_API_KEY (or ANTHROPIC/OPENAI) + TAVILY_API_KEY
    .venv/bin/pytest && .venv/bin/ruff check .

Install the plugin (Claude Code): add this repo as a local marketplace and
enable the `delapan82` plugin; the MCP server runs `python -m delapan82.mcp.server`.

## Surface

| Tool | Does |
|---|---|
| `delapan_resume(query?, depth)` | coverage + rendered preamble + `bundle_path` pointer |
| `delapan_search(query, limit?)` | semantic recall → file pointers (path/excerpt/similarity) |
| `delapan_explore(prompt, max_findings?)` | web research → OKF files, cross-run dedup |

Agents are producers too: Write a conformant file into `knowledge/findings/`
(see `/delapan82:capture`) and the index adopts it on the next read.

## Current state (v1)

Engine + MCP + skills only. Deliberately absent: HTTP API, frontend, Supabase
cloud tier, KG builder / automated concept synthesis (concepts are
agent/human-authored). Design spec: `docs/specs/2026-07-08-delapan-82-okf-native-design.md`.
```

- [ ] **Step 5: Full suite, lint, commit**

```bash
.venv/bin/pytest tests/ -v && .venv/bin/ruff check .
git add -A && git commit -m "test: OKF conformance suite + success-criteria integration chain; README

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
Expected: entire suite green (~28 tests).

---

## Post-plan verification (manual, needs API keys — spec success criteria 1 & 5)

Not a task for the executor loop; run once after Task 12 with real keys in `$ROOT/.env`:

1. In a scratch git repo, run the MCP server and call `delapan_explore` with a small prompt (`max_findings: 3`). Verify: `knowledge/` appears with findings + `index.md` + `log.md`, `git status` shows a clean reviewable diff, and `.index/` is ignored. Re-run the same prompt; verify `merged` is non-empty and no duplicate files appear (criterion 1 + dedup).
2. Open the bundle in Google's reference OKF visualizer (`GoogleCloudPlatform/knowledge-catalog`, static HTML — no backend) and confirm it renders (criterion 5).

## Deferred (explicitly not in this plan, per spec)

- HTTP `/api/*` surface + sigma.js frontend port
- Supabase/cloud tier
- KG builder + automated concept-doc synthesis (concepts are hand-authored in v1)
- LLM-mediated content merge for near-duplicates (v1 merges provenance/confidence only)
- `deepen`, `record_access`, `search_mode` consumers, and other upstream ghosts
```
