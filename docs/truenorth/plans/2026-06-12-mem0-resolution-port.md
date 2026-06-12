# mem0-Style Memory Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a mem0-style resolution step (ADD / UPDATE / NOOP / DELETE) between within-batch finding merge and persistence, so re-exploring overlapping topics updates existing findings instead of appending duplicates.

**Architecture:** A new `delapan/core/memory/` package (models → resolver → persist) sits above the `Store` seam. `resolve_and_persist()` renders + embeds candidate findings, asks an LLM (via the existing AI Gateway) to decide one op per candidate against its top-k existing neighbors, applies the decisions through the `Store` (new `update_finding` + a `resolution_events` log), and returns the affected finding ids. It replaces the duplicated persist block in the two explore call sites. No mem0 dependency; findings only; in-place update with a stable id.

**Vision goals served:** End Goal 1 (self-correcting memory writes) and End Goal 5 (grounding preserved — UPDATE keeps the finding id stable so KG `grounded_in` references stay valid).

**Tech Stack:** Python 3.12, pydantic v2, pydantic-settings, FastAPI, FastMCP, SQLite + sqlite-vec, openai SDK pointed at Vercel AI Gateway, pytest (`asyncio_mode = "auto"`), ruff (line-length 100).

---

## Scope notes (read before starting)

- **Engine repo root is `~/projects/delapan`** (the workspace path `delapan-ai/backend` is a symlink to it). All paths below are relative to that root. Run all commands from `~/projects/delapan`.
- **There is no `SupabaseStore` in this repo.** The store package is `base.py` (the `Store` Protocol) + `sqlite.py` (the only implementation). The cloud tier is future/out-of-repo. This plan adds the new methods to the **Protocol** (so a future `SupabaseStore` must implement them) and to **`SQLiteStore`** only. Do not create a Supabase implementation.
- **Latent quirk (do NOT fix here):** findings persisted from explore store `content` as a rendered markdown *string*, which `SQLiteStore` reads back as `content={}` (only `title` + `similarity` survive on retrieval). The resolver therefore leans on neighbor **titles + similarity**, not neighbor bodies. Candidate bodies are always real (in-memory `Finding`). This is flagged as separate follow-up work.
- **Branch first.** This plan modifies a shared `master`. Create an isolated worktree/branch via `truenorth:using-git-worktrees` before Task 1 if not already in one.
- **TDD + frequent commits.** Every task: failing test → run it red → minimal code → run it green → commit. Run the whole suite with `pytest -q` before each commit after Task 1.

---

### Task 1: `resolution_events` log — Protocol + SQLite

The audit log of resolver decisions. Foundational and isolated.

**Files:**
- Modify: `delapan/store/base.py` (add two Protocol methods near the monitoring section)
- Modify: `delapan/store/sqlite.py` (add table to `_SCHEMA`; add two methods)
- Test: `tests/test_memory_store.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_store.py`:

```python
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_resolution_events_roundtrip(store):
    org, pid = store.resolve_project("rezA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_resolution_events(
        kb,
        [
            {"op": "ADD", "candidate_title": "X", "target_finding_id": None, "reason": "new"},
            {"op": "NOOP", "candidate_title": "Y", "target_finding_id": "f1", "reason": "dup"},
        ],
    )
    evs = store.list_resolution_events(kb)
    assert len(evs) == 2
    assert {e["op"] for e in evs} == {"ADD", "NOOP"}
    assert all("created_at" in e for e in evs)


@pytest.mark.asyncio
async def test_resolution_events_scoped_by_kb(store):
    org, pid = store.resolve_project("rezB", create=True)
    kb1 = store.resolve_kb(org, pid, "main", create=True)
    kb2 = store.resolve_kb(org, pid, "other", create=True)
    await store.insert_resolution_events(kb1, [{"op": "ADD", "candidate_title": "A", "reason": ""}])
    assert len(store.list_resolution_events(kb1)) == 1
    assert store.list_resolution_events(kb2) == []
```

The `store` fixture already exists in `tests/conftest.py` (a local SQLite store on a tmp DB).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_store.py -q`
Expected: FAIL — `AttributeError: 'SQLiteStore' object has no attribute 'insert_resolution_events'`.

- [ ] **Step 3: Add the Protocol methods**

In `delapan/store/base.py`, add at the end of the `Store` class (after `record_access`):

```python
    # --- resolution event log (memory write decisions) -----------------------
    # Append-only observability for the mem0-style resolver: one row per applied
    # decision (ADD/UPDATE/NOOP/DELETE). Never load-bearing for retrieval.

    async def insert_resolution_events(self, kb_id: str, events: list[dict]) -> None:
        """Append resolution decision rows. Best-effort by contract.

        Each row carries ``op, candidate_title, target_finding_id, reason``;
        ``op`` is one of ADD/UPDATE/NOOP/DELETE. No-op on an empty list."""
        ...

    def list_resolution_events(self, kb_id: str, limit: int | None = None) -> list[dict]:
        """Most-recent resolution events in ``kb_id`` (newest first). Rows carry
        ``id, op, candidate_title, target_finding_id, reason, created_at``."""
        ...
```

- [ ] **Step 4: Add the SQLite table**

In `delapan/store/sqlite.py`, inside the `_SCHEMA` string, add (just before the closing `"""`, after the `kg_schemas` block):

```sql
CREATE TABLE IF NOT EXISTS resolution_events (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL,
  op TEXT NOT NULL, candidate_title TEXT, target_finding_id TEXT,
  reason TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_resolution_events_kb ON resolution_events(kb_id);
```

`_ensure_schema` runs `executescript(_SCHEMA)` on every open, and every statement is `IF NOT EXISTS`, so this auto-creates the table on existing DBs with no migration.

- [ ] **Step 5: Add the SQLite methods**

In `delapan/store/sqlite.py`, add to `SQLiteStore` (place near `record_access`, the monitoring section):

```python
    # --- resolution event log ------------------------------------------------

    async def insert_resolution_events(self, kb_id: str, events: list[dict]) -> None:
        """Append resolution decision rows; org forced ``"local"``. No-op when empty."""
        if not events:
            return
        for e in events:
            self._conn.execute(
                """
                INSERT INTO resolution_events
                  (id, org_id, kb_id, op, candidate_title, target_finding_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    uuid.uuid4().hex,
                    _ORG,
                    kb_id,
                    e.get("op"),
                    e.get("candidate_title"),
                    e.get("target_finding_id"),
                    e.get("reason"),
                    _now_iso(),
                ),
            )
        self._conn.commit()

    def list_resolution_events(self, kb_id: str, limit: int | None = None) -> list[dict]:
        """Most-recent resolution events for `kb_id`, newest first (cap 500)."""
        n = min(limit or 50, 500)
        rows = self._conn.execute(
            "SELECT id, op, candidate_title, target_finding_id, reason, created_at "
            "FROM resolution_events WHERE kb_id = ? ORDER BY created_at DESC LIMIT ?;",
            (kb_id, n),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "op": r["op"],
                "candidate_title": r["candidate_title"],
                "target_finding_id": r["target_finding_id"],
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
```

`uuid` and `_now_iso` are already imported/defined at the top of `sqlite.py`.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_memory_store.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add delapan/store/base.py delapan/store/sqlite.py tests/test_memory_store.py
git commit -m "feat(store): resolution_events log (Protocol + SQLite)"
```

---

### Task 2: `Store.update_finding` — Protocol + SQLite

In-place finding overwrite with a stable id (the capability the Protocol lacks today).

**Files:**
- Modify: `delapan/store/base.py` (add Protocol method in the findings section)
- Modify: `delapan/store/sqlite.py` (add method in the findings section)
- Test: `tests/test_memory_store.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memory_store.py`:

```python
@pytest.mark.asyncio
async def test_update_finding_in_place_keeps_id(store):
    org, pid = store.resolve_project("updA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    emb0 = [0.01] * 1536
    ids = await store.insert_findings(
        [
            {
                "org_id": org,
                "kb_id": kb,
                "title": "old",
                "content": {"k": "v0"},
                "category": "fact",
                "confidence": 0.4,
                "tags": [],
                "provenance": [{"url": "a"}],
                "embedding": emb0,
            }
        ]
    )
    fid = ids[0]
    emb1 = [0.5] * 1536
    await store.update_finding(
        kb,
        fid,
        content={"k": "v1"},
        confidence=0.8,
        provenance=[{"url": "a"}, {"url": "b"}],
        embedding=emb1,
        title="new",
    )
    got = store.get_finding(kb, fid)
    assert got["id"] == fid                 # id is stable
    assert got["title"] == "new"
    assert got["confidence"] == 0.8
    assert got["content"] == {"k": "v1"}    # dict content round-trips
    assert len(got["provenance"]) == 2
    assert store.count_findings(kb) == 1    # overwrite, not a new row
    hits = await store.match_findings(kb, emb1, match_count=1, min_similarity=0.0)
    assert hits[0]["id"] == fid             # vector re-indexed to emb1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_store.py::test_update_finding_in_place_keeps_id -q`
Expected: FAIL — `AttributeError: ... 'update_finding'`.

- [ ] **Step 3: Add the Protocol method**

In `delapan/store/base.py`, in the findings section right after `insert_findings`, add:

```python
    async def update_finding(
        self,
        kb_id: str,
        finding_id: str,
        *,
        content,
        confidence,
        provenance,
        embedding,
        title: str | None = None,
    ) -> None:
        """Overwrite a finding in place, keeping its id STABLE (so KG `grounded_in`
        references stay valid). Replaces content/confidence/provenance and
        re-indexes the embedding; renames the title when given. The caller computes
        the merged values — this is a straight overwrite, not a merge."""
        ...
```

- [ ] **Step 4: Add the SQLite implementation**

In `delapan/store/sqlite.py`, in the findings section right after `insert_findings`, add:

```python
    async def update_finding(
        self,
        kb_id: str,
        finding_id: str,
        *,
        content,
        confidence,
        provenance,
        embedding,
        title: str | None = None,
    ) -> None:
        """In-place overwrite + re-embed; id stays stable. JSON-encodes content/
        provenance; replaces the ``vec_findings`` row when an embedding is given."""
        sets = ["content = ?", "confidence = ?", "provenance = ?"]
        vals: list[object] = [
            _json_dump_maybe(content),
            confidence,
            json.dumps(list(provenance or [])),
        ]
        if title is not None:
            sets.append("title = ?")
            vals.append(title)
        self._conn.execute(
            f"UPDATE findings SET {', '.join(sets)} WHERE id = ? AND kb_id = ?;",
            (*vals, finding_id, kb_id),
        )
        if embedding is not None:
            self._conn.execute("DELETE FROM vec_findings WHERE finding_id = ?;", (finding_id,))
            self._conn.execute(
                "INSERT INTO vec_findings (finding_id, embedding) VALUES (?, ?);",
                (finding_id, serialize_float32(list(embedding))),
            )
        self._conn.commit()
```

`_json_dump_maybe`, `json`, and `serialize_float32` are already imported/defined in `sqlite.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_memory_store.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add delapan/store/base.py delapan/store/sqlite.py tests/test_memory_store.py
git commit -m "feat(store): update_finding in-place overwrite (Protocol + SQLite)"
```

---

### Task 3: `MemoryConfig` + `config.yaml` section

The tunable knobs + the `enabled` kill-switch.

**Files:**
- Modify: `delapan/core/config.py` (add `MemoryConfig`, add field to `AppConfig`)
- Modify: `config.yaml` (add a `memory:` block)
- Test: `tests/test_config.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_memory_config_defaults_and_env_override(monkeypatch):
    from delapan.core.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    assert cfg.memory.enabled is True
    assert cfg.memory.neighbor_top_k == 5
    assert cfg.memory.resolution_model == "anthropic/claude-sonnet-4.6"

    monkeypatch.setenv("DLP_MEMORY__ENABLED", "false")
    get_config.cache_clear()
    assert get_config().memory.enabled is False
    get_config.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_memory_config_defaults_and_env_override -q`
Expected: FAIL — `AttributeError: 'AppConfig' object has no attribute 'memory'`.

- [ ] **Step 3: Add `MemoryConfig` and wire it into `AppConfig`**

In `delapan/core/config.py`, add this class just before `class PromptsConfig`:

```python
class MemoryConfig(BaseModel):
    """mem0-style finding resolution: decide ADD/UPDATE/NOOP/DELETE per candidate
    against the top-k existing findings before persisting. Disabled → pure ADD
    (today's append behavior). Models are AI Gateway slugs (dots for versions)."""

    enabled: bool = True  # kill-switch: false → pure ADD, no resolver call
    resolution_model: str = "anthropic/claude-sonnet-4.6"
    resolution_fallback_model: str = "openai/gpt-5.4-mini"
    temperature: float = 0.0
    neighbor_top_k: int = 5  # existing findings retrieved per candidate
    neighbor_min_similarity: float = 0.6  # floor for a neighbor to be considered
    max_candidates_per_pass: int = 25  # batch cap for one LLM resolution pass
    reasoning_effort: str | None = None  # gateway/Gemini thinking level
```

Then add the field to `class AppConfig` (place it right after the `knowledge_graph` line):

```python
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
```

- [ ] **Step 4: Add the `config.yaml` block**

Append to `config.yaml` (top-level, after the `knowledge_graph:` section). The dataclass defaults already cover everything, but mirror the other sections so the knobs are discoverable:

```yaml
# Memory resolution — decide ADD/UPDATE/NOOP/DELETE per new finding against the
# top-k existing findings before persisting. enabled=false → pure ADD (append).
memory:
  enabled: true
  resolution_model: anthropic/claude-sonnet-4.6   # cheaper than the KG's Opus; runs every explore
  resolution_fallback_model: openai/gpt-5.4-mini
  temperature: 0.0
  neighbor_top_k: 5                 # existing findings retrieved per candidate
  neighbor_min_similarity: 0.6      # floor for a neighbor to be considered
  max_candidates_per_pass: 25       # batch cap for one LLM resolution pass
  reasoning_effort: null
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config.py -q`
Expected: PASS (all config tests, including the new one).

- [ ] **Step 6: Commit**

```bash
git add delapan/core/config.py config.yaml tests/test_config.py
git commit -m "feat(config): memory resolution config section + kill-switch"
```

---

### Task 4: `core/memory/models.py` — decision models

The typed contract between resolver and persist (also the LLM structured-output schema).

**Files:**
- Create: `delapan/core/memory/__init__.py`
- Create: `delapan/core/memory/models.py`
- Test: `tests/test_memory_models.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_models.py`:

```python
from __future__ import annotations


def test_resolution_models_basic():
    from delapan.core.memory.models import (
        ResolutionBatch,
        ResolutionDecision,
        ResolutionOp,
        ResolutionOutcome,
    )

    b = ResolutionBatch(
        decisions=[ResolutionDecision(candidate_index=0, op=ResolutionOp.ADD)]
    )
    assert b.decisions[0].op == ResolutionOp.ADD
    assert b.decisions[0].target_finding_id is None

    # round-trips through JSON (it is an LLM structured-output schema)
    parsed = ResolutionBatch.model_validate_json(b.model_dump_json())
    assert parsed.decisions[0].candidate_index == 0

    out = ResolutionOutcome(affected_finding_ids=["a", "b"])
    assert out.affected_finding_ids == ["a", "b"]
    assert out.events == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.core.memory'`.

- [ ] **Step 3: Create the package init**

Create `delapan/core/memory/__init__.py`:

```python
"""mem0-style finding resolution (ADD/UPDATE/NOOP/DELETE) behind the Store seam."""
```

- [ ] **Step 4: Create the models**

Create `delapan/core/memory/models.py`:

```python
"""Resolution decision models — the typed contract between resolver and persist.

    resolver → [ResolutionDecision…]   persist applies them → ResolutionOutcome

``ResolutionBatch`` doubles as the LLM structured-output schema (one decision per
candidate, keyed by ``candidate_index``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ResolutionOp(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"
    DELETE = "DELETE"


class ResolutionDecision(BaseModel):
    """One decision for one candidate, identified by its batch position."""

    candidate_index: int = Field(description="0-based index into the candidate batch")
    op: ResolutionOp
    target_finding_id: str | None = Field(
        default=None,
        description="Existing finding id to UPDATE/NOOP/DELETE; null for ADD",
    )
    reason: str = Field(default="", description="Short justification for the op")


class ResolutionBatch(BaseModel):
    """Structured-output container — one decision per candidate needing one."""

    decisions: list[ResolutionDecision] = Field(default_factory=list)


class ResolutionEvent(BaseModel):
    """A log row describing one applied decision (persisted to resolution_events)."""

    op: str
    candidate_title: str
    target_finding_id: str | None = None
    reason: str = ""


class ResolutionOutcome(BaseModel):
    """Result of resolve_and_persist: finding ids touched + the events logged."""

    affected_finding_ids: list[str] = Field(default_factory=list)
    events: list[ResolutionEvent] = Field(default_factory=list)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_memory_models.py -q`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add delapan/core/memory/__init__.py delapan/core/memory/models.py tests/test_memory_models.py
git commit -m "feat(memory): resolution decision models"
```

---

### Task 5: `core/exploration/render.py` — lift shared helpers

Extract the finding-render helpers duplicated in `mcp/server.py` and `routes_explore.py` into one module (the resolver and persist will use them; the call sites switch over in Task 8).

**Files:**
- Create: `delapan/core/exploration/render.py`
- Test: `tests/test_render.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_render.py`:

```python
from __future__ import annotations


def test_render_content_single_string_value():
    from delapan.core.exploration.render import render_content

    assert render_content({"summary": "hello"}) == "hello"


def test_render_content_multi_key_markdown():
    from delapan.core.exploration.render import render_content

    out = render_content({"plan": "free", "limit": 1000})
    assert "**Plan**: free" in out
    assert "**Limit**: 1000" in out


def test_render_content_passthrough_and_empty():
    from delapan.core.exploration.render import render_content

    assert render_content("already text") == "already text"
    assert render_content({}) == ""


def test_normalize_provenance_stamps_accessed_at():
    from delapan.core.exploration.render import normalize_provenance

    out = normalize_provenance([{"url": "http://x"}])
    assert out[0]["url"] == "http://x"
    assert "accessed_at" in out[0]
    assert normalize_provenance([]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_render.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.core.exploration.render'`.

- [ ] **Step 3: Create the module**

Create `delapan/core/exploration/render.py` (bodies lifted verbatim from `delapan/mcp/server.py`):

```python
"""Render a finding's content for persistence.

Shared by the explore call sites (`mcp/server.py`, `api/routes_explore.py`) and
the memory persist path. Previously duplicated verbatim in both call sites.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_content(content: Any) -> str:
    """Render a finding's free-form ``content`` dict to a markdown body.

    A single-key dict with a string value renders to that string; otherwise each
    key becomes a ``**Label**: value`` line (lists/dicts as fenced JSON). Strings
    pass through; non-dicts are stringified."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_render.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add delapan/core/exploration/render.py tests/test_render.py
git commit -m "refactor(exploration): extract shared render_content/normalize_provenance"
```

---

### Task 6: `core/memory/resolver.py` — the decision brain

Retrieve neighbors + one LLM pass → one decision per candidate. Defaults to ADD on any failure; never drops a finding.

**Files:**
- Create: `delapan/core/memory/resolver.py`
- Test: `tests/test_memory_resolver.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_resolver.py`:

```python
from __future__ import annotations

import pytest

from delapan.core.config import MemoryConfig
from delapan.core.exploration.models import Finding
from delapan.core.memory import resolver as resolver_mod
from delapan.core.memory.models import ResolutionBatch, ResolutionDecision, ResolutionOp


def _finding(title, content):
    return Finding(
        exploration_id="e", project_id="p", category="fact", title=title, content=content
    )


class _FakeStore:
    """Returns a preset neighbor list per match_findings call, in candidate order."""

    def __init__(self, neighbors_in_order):
        self._n = neighbors_in_order
        self.calls = 0

    async def match_findings(self, kb_id, query_embedding, match_count, min_similarity, categories=None):
        out = self._n[self.calls]
        self.calls += 1
        return out


@pytest.mark.asyncio
async def test_no_neighbors_short_circuits_to_add(monkeypatch):
    async def _never(**kwargs):
        raise AssertionError("LLM must not be called when no candidate has neighbors")

    monkeypatch.setattr(resolver_mod, "structured_completion", _never)
    store = _FakeStore([[], []])
    cands = [_finding("A", {"k": "1"}), _finding("B", {"k": "2"})]
    embs = [[0.0] * 1536, [0.0] * 1536]
    out = await resolver_mod.resolve(store, "kb", cands, embs, MemoryConfig())
    assert [d.op for d in out] == [ResolutionOp.ADD, ResolutionOp.ADD]


@pytest.mark.asyncio
async def test_update_decision_applied(monkeypatch):
    async def _fake(**kwargs):
        return ResolutionBatch(
            decisions=[
                ResolutionDecision(
                    candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id="f1", reason="refines"
                )
            ]
        )

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.UPDATE
    assert out[0].target_finding_id == "f1"


@pytest.mark.asyncio
async def test_unknown_target_demoted_to_add(monkeypatch):
    async def _fake(**kwargs):
        return ResolutionBatch(
            decisions=[
                ResolutionDecision(
                    candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id="ghost", reason="x"
                )
            ]
        )

    monkeypatch.setattr(resolver_mod, "structured_completion", _fake)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.ADD


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_add(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(resolver_mod, "structured_completion", _boom)
    store = _FakeStore([[{"id": "f1", "title": "A", "similarity": 0.9}]])
    out = await resolver_mod.resolve(store, "kb", [_finding("A", {"k": "1"})], [[0.1] * 1536], MemoryConfig())
    assert out[0].op == ResolutionOp.ADD
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_resolver.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.core.memory.resolver'`.

- [ ] **Step 3: Create the resolver**

Create `delapan/core/memory/resolver.py`:

```python
"""Memory resolver — decide ADD/UPDATE/NOOP/DELETE per candidate finding.

    candidates + neighbors ─► one LLM pass ─► [ResolutionDecision…]

Ported pattern from mem0's update-memory step (no mem0 dependency): for each new
candidate, retrieve the top-k semantically similar EXISTING findings and let the
model decide whether it is new (ADD), refines one (UPDATE), duplicates one (NOOP),
or contradicts one (DELETE). Tier-agnostic: uses only the Store contract.

Neighbor bodies are unreliable on the local tier (explore-persisted findings read
back with content={}), so the prompt leans on neighbor TITLES + similarity;
candidate bodies are always real. Any failure defaults to ADD — a resolver
failure must never drop a finding.
"""

from __future__ import annotations

import logging

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import MemoryConfig
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import render_content
from delapan.core.memory.models import ResolutionBatch, ResolutionDecision, ResolutionOp
from delapan.store import Store

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You maintain a knowledge base of findings. For each new CANDIDATE finding you "
    "are shown its most semantically-similar EXISTING findings (id, title, similarity). "
    "Decide ONE operation per candidate:\n"
    "- ADD: genuinely new information (no existing finding covers it).\n"
    "- UPDATE: refines/supersedes one existing finding (set target_finding_id to its id).\n"
    "- NOOP: duplicates one existing finding, no new info (set target_finding_id to its id).\n"
    "- DELETE: directly contradicts and invalidates one existing finding "
    "(set target_finding_id to its id). Use sparingly.\n"
    "Return one decision per candidate using the candidate_index you were given. "
    "When unsure, prefer ADD. Only UPDATE/NOOP/DELETE when a neighbor is clearly the same topic."
)


def _candidate_block(i: int, f: Finding, neighbors: list[dict]) -> str:
    body = render_content(f.content)[:600]
    lines = [f"### candidate_index={i}", f"title: {f.title}", f"body: {body}", "neighbors:"]
    if not neighbors:
        lines.append("  (none)")
    for n in neighbors:
        sim = round(float(n.get("similarity", 0.0)), 3)
        lines.append(f"  - id={n.get('id')} sim={sim} title={n.get('title')!r}")
    return "\n".join(lines)


async def resolve(
    store: Store,
    kb_id: str,
    candidates: list[Finding],
    embeddings: list[list[float]],
    cfg: MemoryConfig,
) -> list[ResolutionDecision]:
    """One decision per candidate (same order + length as ``candidates``).

    Candidates with no neighbor above the similarity floor short-circuit to ADD
    without consuming an LLM slot. Any LLM/parse failure → all-ADD fallback."""
    if not candidates:
        return []

    neighbor_sets: list[list[dict]] = []
    for emb in embeddings:
        try:
            hits = await store.match_findings(
                kb_id,
                emb,
                match_count=cfg.neighbor_top_k,
                min_similarity=cfg.neighbor_min_similarity,
            )
        except Exception:  # noqa: BLE001 — retrieval failure → treat as no neighbors
            hits = []
        neighbor_sets.append(hits)

    # Default everything to ADD; only candidates with neighbors need the LLM.
    decisions: list[ResolutionDecision] = [
        ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD, reason="no similar finding")
        for i in range(len(candidates))
    ]
    need_llm = [i for i, ns in enumerate(neighbor_sets) if ns]
    if not need_llm:
        return decisions

    user = "\n\n".join(_candidate_block(i, candidates[i], neighbor_sets[i]) for i in need_llm)
    try:
        batch = await structured_completion(
            model=cfg.resolution_model,
            response_format=ResolutionBatch,
            system=_SYSTEM,
            user=user,
            temperature=cfg.temperature,
            fallback_model=cfg.resolution_fallback_model,
            reasoning_effort=cfg.reasoning_effort,
            use_json_schema=True,
        )
    except Exception as exc:  # noqa: BLE001 — never drop findings on resolver failure
        logger.warning("memory resolution failed (%s); falling back to ADD-all", exc)
        return decisions

    valid = set(need_llm)
    for d in batch.decisions:
        i = d.candidate_index
        if i not in valid:
            continue
        if d.op in (ResolutionOp.UPDATE, ResolutionOp.NOOP, ResolutionOp.DELETE):
            neighbor_ids = {n.get("id") for n in neighbor_sets[i]}
            if d.target_finding_id not in neighbor_ids:
                # Model named a target that wasn't offered — demote to a safe ADD.
                decisions[i] = ResolutionDecision(
                    candidate_index=i,
                    op=ResolutionOp.ADD,
                    reason="model named unknown target; demoted to ADD",
                )
                continue
        decisions[i] = d
    return decisions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_memory_resolver.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add delapan/core/memory/resolver.py tests/test_memory_resolver.py
git commit -m "feat(memory): resolver — ADD/UPDATE/NOOP/DELETE with ADD-all fallback"
```

---

### Task 7: `core/memory/persist.py` — resolve + apply + log

The orchestrator both call sites will use. This task also covers vision Acceptance Criterion #2 (re-ingest overlapping → UPDATE, no duplicate, logged).

**Files:**
- Create: `delapan/core/memory/persist.py`
- Test: `tests/test_memory_persist.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_persist.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.core.config import get_config
from delapan.core.exploration.models import Finding
from delapan.core.memory import persist as persist_mod
from delapan.core.memory.models import ResolutionDecision, ResolutionOp


def _finding(pid, title, content):
    return Finding(exploration_id="e", project_id=pid, category="fact", title=title, content=content)


@pytest.mark.asyncio
async def test_add_then_update_no_duplicate(store, monkeypatch):
    org, pid = store.resolve_project("perA", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)

    async def _fake_embed(texts):
        return [[0.01] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)
    get_config.cache_clear()
    cfg = get_config()

    # First pass — resolver says ADD.
    async def _all_add(store_, kb_, cands, embs, mcfg):
        return [ResolutionDecision(candidate_index=i, op=ResolutionOp.ADD) for i in range(len(cands))]

    monkeypatch.setattr(persist_mod, "resolve", _all_add)
    f1 = _finding(pid, "Tavily pricing", {"k": "free 1000/mo"})
    out1 = await persist_mod.resolve_and_persist(ctx, store, [f1], cfg)
    assert len(out1.affected_finding_ids) == 1
    assert store.count_findings(kb) == 1
    fid = out1.affected_finding_ids[0]

    # Second pass — overlapping candidate, resolver says UPDATE the existing one.
    async def _update(store_, kb_, cands, embs, mcfg):
        return [
            ResolutionDecision(
                candidate_index=0, op=ResolutionOp.UPDATE, target_finding_id=fid, reason="refine"
            )
        ]

    monkeypatch.setattr(persist_mod, "resolve", _update)
    f2 = _finding(pid, "Tavily pricing (updated)", {"k": "free + paid tiers"})
    out2 = await persist_mod.resolve_and_persist(ctx, store, [f2], cfg)
    assert out2.affected_finding_ids == [fid]   # same id (stable)
    assert store.count_findings(kb) == 1        # NO duplicate row
    assert store.get_finding(kb, fid)["title"] == "Tavily pricing (updated)"
    evs = store.list_resolution_events(kb)
    assert any(e["op"] == "UPDATE" for e in evs)


@pytest.mark.asyncio
async def test_disabled_is_pure_add(store, monkeypatch):
    org, pid = store.resolve_project("perB", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)

    async def _fake_embed(texts):
        return [[0.02] * 1536 for _ in texts]

    monkeypatch.setattr(persist_mod, "embed_batch", _fake_embed)

    async def _boom(*a, **k):
        raise AssertionError("resolver must not run when memory.enabled is false")

    monkeypatch.setattr(persist_mod, "resolve", _boom)

    monkeypatch.setenv("DLP_MEMORY__ENABLED", "false")
    get_config.cache_clear()
    cfg = get_config()
    f = _finding(pid, "X", {"k": "v"})
    out = await persist_mod.resolve_and_persist(ctx, store, [f], cfg)
    assert store.count_findings(kb) == 1
    assert len(out.affected_finding_ids) == 1
    assert store.list_resolution_events(kb) == []  # disabled path logs nothing
    get_config.cache_clear()


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty(store):
    org, pid = store.resolve_project("perC", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    ctx = SimpleNamespace(org_id=org, kb_id=kb, project_id=pid)
    get_config.cache_clear()
    out = await persist_mod.resolve_and_persist(ctx, store, [], get_config())
    assert out.affected_finding_ids == []
    assert store.count_findings(kb) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_persist.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.core.memory.persist'`.

- [ ] **Step 3: Create the persist orchestrator**

Create `delapan/core/memory/persist.py`:

```python
"""resolve_and_persist — the single finding-persist path.

    candidates ─► render+embed ─► resolve ─► apply (insert/update/delete) ─► log
                                                         └─► ResolutionOutcome

Replaces the duplicated persist block in the explore call sites. With
``memory.enabled is False`` (or no candidates) it is pure ADD — byte-for-byte
today's append behavior.
"""

from __future__ import annotations

import logging

from delapan.core.agent.state import TenantContext
from delapan.core.clients.embeddings import embed_batch
from delapan.core.config import AppConfig
from delapan.core.exploration.merger import confidence_from_sources
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import normalize_provenance, render_content
from delapan.core.memory.models import ResolutionEvent, ResolutionOp, ResolutionOutcome
from delapan.core.memory.resolver import resolve
from delapan.store import Store

logger = logging.getLogger(__name__)


def _row_from_candidate(ctx: TenantContext, f: Finding, embedding: list[float]) -> dict:
    """Build an insert_findings row from a candidate Finding (parity with the old
    explore persist block: content rendered to a markdown body)."""
    return {
        "org_id": ctx.org_id,
        "kb_id": ctx.kb_id,
        "title": f.title,
        "content": render_content(f.content),
        "category": f.category,
        "confidence": float(f.confidence) if f.confidence is not None else None,
        "tags": list(f.tags or []),
        "provenance": normalize_provenance(f.provenance),
        "embedding": embedding,
    }


def _merge_provenance(existing: list[dict], new: list[dict]) -> list[dict]:
    """Union by url (existing first), preserving non-url entries."""
    seen: set[str] = set()
    out: list[dict] = []
    for p in [*(existing or []), *(new or [])]:
        url = p.get("url", "") if isinstance(p, dict) else ""
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        out.append(p)
    return out


async def resolve_and_persist(
    ctx: TenantContext, store: Store, candidates: list[Finding], cfg: AppConfig
) -> ResolutionOutcome:
    """Resolve each candidate against existing findings, then apply + log.

    Returns the affected finding ids (added ∪ updated) for the synopsis/KG
    schedulers. NOOP/DELETE contribute no affected ids."""
    if not candidates:
        return ResolutionOutcome()

    contents = [render_content(f.content) for f in candidates]
    embeddings = await embed_batch(contents)

    # Kill-switch / fast path: pure ADD, no resolution, no events.
    if not cfg.memory.enabled:
        rows = [_row_from_candidate(ctx, f, emb) for f, emb in zip(candidates, embeddings)]
        ids = await store.insert_findings(rows)
        return ResolutionOutcome(affected_finding_ids=ids)

    decisions = await resolve(store, ctx.kb_id, candidates, embeddings, cfg.memory)

    affected: list[str] = []
    events: list[ResolutionEvent] = []
    add_rows: list[dict] = []
    for f, emb, d in zip(candidates, embeddings, decisions):
        if d.op == ResolutionOp.ADD:
            add_rows.append(_row_from_candidate(ctx, f, emb))
            events.append(ResolutionEvent(op="ADD", candidate_title=f.title, reason=d.reason))
        elif d.op == ResolutionOp.NOOP:
            events.append(
                ResolutionEvent(
                    op="NOOP",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    reason=d.reason,
                )
            )
        elif d.op == ResolutionOp.DELETE:
            try:
                store.delete_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — a stale target is not fatal
                logger.debug("resolution DELETE: target %s absent", d.target_finding_id)
            events.append(
                ResolutionEvent(
                    op="DELETE",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    reason=d.reason,
                )
            )
        elif d.op == ResolutionOp.UPDATE:
            try:
                target = store.get_finding(ctx.kb_id, d.target_finding_id)
            except Exception:  # noqa: BLE001 — target vanished → ADD instead
                add_rows.append(_row_from_candidate(ctx, f, emb))
                events.append(
                    ResolutionEvent(
                        op="ADD", candidate_title=f.title, reason="update target missing; added"
                    )
                )
                continue
            merged_prov = _merge_provenance(target.get("provenance") or [], normalize_provenance(f.provenance))
            source_count = len({p.get("url") for p in merged_prov if p.get("url")}) or 1
            await store.update_finding(
                ctx.kb_id,
                d.target_finding_id,
                content=render_content(f.content),
                confidence=confidence_from_sources(source_count),
                provenance=merged_prov,
                embedding=emb,
                title=f.title,
            )
            affected.append(d.target_finding_id)
            events.append(
                ResolutionEvent(
                    op="UPDATE",
                    candidate_title=f.title,
                    target_finding_id=d.target_finding_id,
                    reason=d.reason,
                )
            )

    if add_rows:
        affected.extend(await store.insert_findings(add_rows))
    if events:
        await store.insert_resolution_events(ctx.kb_id, [e.model_dump() for e in events])
    return ResolutionOutcome(affected_finding_ids=affected, events=events)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_memory_persist.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS (all tests; nothing regressed).

- [ ] **Step 6: Commit**

```bash
git add delapan/core/memory/persist.py tests/test_memory_persist.py
git commit -m "feat(memory): resolve_and_persist — apply decisions + log (acceptance #2)"
```

---

### Task 8: Wire the two explore call sites

Replace the duplicated persist block with `resolve_and_persist`; remove the now-orphaned local helpers/imports.

**Files:**
- Modify: `delapan/api/routes_explore.py:130-152` (persist block), imports, and the local helper defs
- Modify: `delapan/mcp/server.py:156-181` (persist block), imports, and the local helper defs

- [ ] **Step 1: Rewire `routes_explore.py`**

Add to the imports (top of file, near the other `delapan.core` imports):

```python
from delapan.core.memory.persist import resolve_and_persist
```

Remove this import line (its only use is the persist block being replaced):

```python
from delapan.core.clients.embeddings import embed_batch
```

Replace the persist block — these lines:

```python
        ids: list[str] = []
        if captured:
            rows: list[dict] = []
            contents: list[str] = []
            for f in captured:
                rendered = _render_content(f.content)
                rows.append(
                    {
                        "org_id": ctx.org_id,
                        "kb_id": ctx.kb_id,
                        "title": f.title,
                        "content": rendered,
                        "category": f.category,
                        "confidence": (float(f.confidence) if f.confidence is not None else None),
                        "tags": list(f.tags or []),
                        "provenance": _normalize_provenance(f.provenance),
                    }
                )
                contents.append(rendered)
            embeddings = await embed_batch(contents)
            for row, emb in zip(rows, embeddings):
                row["embedding"] = emb
            ids = await store.insert_findings(rows)
```

with:

```python
        outcome = await resolve_and_persist(ctx, store, captured, get_config())
        ids = outcome.affected_finding_ids
```

Then delete the now-unused local helper definitions `def _render_content(...)` and `def _normalize_provenance(...)` from this file. **Keep** `def _now_iso(...)` (still used by `update_exploration`).

- [ ] **Step 2: Rewire `mcp/server.py`**

Add to the imports:

```python
from delapan.core.memory.persist import resolve_and_persist
```

Change this import to drop `embed_batch` (keep `embed_text`, used by search):

```python
from delapan.core.clients.embeddings import embed_batch, embed_text
```

to:

```python
from delapan.core.clients.embeddings import embed_text
```

Replace the persist block — these lines:

```python
        ids: list[str] = []
        if captured:
            # Reuse tools/explore.py's row-building + embedding sequence: render
            # each content dict to a markdown body, embed the bodies in one batch,
            # then build rows matching the Store's insert_findings shape.
            rows: list[dict] = []
            contents: list[str] = []
            for f in captured:
                body = _render_content(f.content)
                rows.append(
                    {
                        "org_id": ctx.org_id,
                        "kb_id": ctx.kb_id,
                        "title": f.title,
                        "content": body,
                        "category": f.category,
                        "confidence": (float(f.confidence) if f.confidence is not None else None),
                        "tags": list(f.tags or []),
                        "provenance": _normalize_provenance(f.provenance),
                    }
                )
                contents.append(body)
            embeddings = await embed_batch(contents)
            for row, emb in zip(rows, embeddings):
                row["embedding"] = emb
            ids = await store.insert_findings(rows)
```

with:

```python
        outcome = await resolve_and_persist(ctx, store, captured, get_config())
        ids = outcome.affected_finding_ids
```

Then delete the now-unused `def _render_content(...)` and `def _normalize_provenance(...)` from this file. **Keep** `def _now_iso(...)` (4 uses remain) and the `Any` import if still referenced elsewhere.

- [ ] **Step 3: Verify no orphaned references remain**

Run:
```bash
grep -n "_render_content\|_normalize_provenance\|embed_batch" delapan/api/routes_explore.py delapan/mcp/server.py
```
Expected: **no output** (both helpers and `embed_batch` fully removed from both files).

Run the linter:
```bash
ruff check delapan/api/routes_explore.py delapan/mcp/server.py
```
Expected: no unused-import or undefined-name errors. Fix any flagged unused import (e.g. drop `Any` from `mcp/server.py` if it is now unused — confirm with `grep -n "Any" delapan/mcp/server.py` first).

- [ ] **Step 4: Run the full suite**

Run: `pytest -q`
Expected: PASS. In particular `tests/test_mcp_smoke.py` and `tests/test_api_routes.py` stay green — they exercise resume/search/projects and the explore error paths, none of which run the persist block with real findings.

- [ ] **Step 5: Commit**

```bash
git add delapan/api/routes_explore.py delapan/mcp/server.py
git commit -m "refactor(explore): route persistence through resolve_and_persist (DRY)"
```

---

## Self-Review

**Spec coverage** (against `docs/truenorth/specs/2026-06-12-mem0-resolution-port-design.md`):

| Spec item | Task |
|---|---|
| `core/memory/models.py` (ResolutionOp/Decision/Event/Outcome) | Task 4 |
| `core/memory/resolver.py` (neighbors + one LLM pass) | Task 6 |
| `core/memory/persist.py` (resolve_and_persist) | Task 7 |
| `Store.update_finding` (in-place, stable id) | Task 2 |
| `resolution_events` log + insert/list | Task 1 |
| `memory:` config + `enabled` kill-switch | Task 3 |
| v1 UPDATE semantics (candidate content wins, provenance union, confidence recompute, candidate embedding) | Task 7 (`resolve_and_persist` UPDATE branch) |
| ADD-all fallback on LLM failure | Task 6 |
| DELETE conservative + tolerates missing target | Task 7 |
| Collapse duplicated persist blocks; lift `_render_content`/`_normalize_provenance` | Tasks 5 + 8 |
| Integration: re-ingest overlapping → UPDATE, no duplicate, logged (Acceptance #2) | Task 7 (`test_add_then_update_no_duplicate`) |
| Local-tier suite green, no services | Task 7 Step 5 + Task 8 Step 4 |

**Deviation from spec (flagged):** the spec assumed "both tiers (SQLite + Supabase)." `SupabaseStore` does not exist in this repo, so the plan implements the Protocol + SQLite only; a future cloud store must satisfy the new Protocol methods. The spec's `resolution_fallback_model` was written as Gemini; the plan uses `openai/gpt-5.4-mini` to match the repo's existing `extraction_fallback_model` convention.

**Placeholder scan:** none — every code step shows complete code; every run step shows the exact command + expected result.

**Type consistency:** `resolve()` signature `(store, kb_id, candidates, embeddings, cfg: MemoryConfig)` is identical in Task 6 (definition), Task 6 tests, and Task 7 (call + monkeypatch). `resolve_and_persist(ctx, store, candidates, cfg: AppConfig)` is identical in Task 7 and both Task 8 call sites. `update_finding(kb_id, finding_id, *, content, confidence, provenance, embedding, title=None)` matches across Task 2 (Protocol + SQLite + test) and Task 7's UPDATE branch. `ResolutionEvent.model_dump()` keys (`op, candidate_title, target_finding_id, reason`) match `insert_resolution_events`'s `e.get(...)` reads in Task 1.

## Out of scope (follow-ups)

- **Elasticsearch backend** (vision detour 2) — the resolver is backend-agnostic; ES slots under the Store seam later.
- **`SupabaseStore`** implementation of the three new Protocol methods (when the cloud tier lands).
- **Content round-trip quirk** — explore-persisted `content` reads back as `{}` from `SQLiteStore`; the resolver works around it via titles + similarity. Worth a separate fix (store content as JSON, or read rendered strings back as-is).
