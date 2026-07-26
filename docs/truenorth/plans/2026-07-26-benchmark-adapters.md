# Benchmark Adapters (Phase 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run watsonxDocsQA and MultiHop-RAG through the existing eval harness by adapting each dataset's corpus + grounded QA into a local KB and a `load_question_set`-compatible yaml, per the approved spec `docs/truenorth/specs/2026-07-26-benchmark-adapter-design.md`.

**Architecture:** New `evals/adapters/` package. Docs are chunked (~1,000 chars) and inserted as findings with **deterministic ids** (sha1 of doc_id + chunk index) via `store.insert_findings` — no extraction, no resolver — so gold mapping is computable before insert. Gold = chunks of the dataset's gold docs, narrowed to evidence-containing chunks when evidence text exists (MultiHop-RAG). One harness change: an `oracle_budget` override so chunked gold sets don't get clipped by the 7,000-char preamble budget.

**Vision goals served:** *Grounding preserved end to end* (external benchmarks exercise the same provenance-carrying injection path).

**Tech Stack:** Python 3.12, `datasets`+`huggingface_hub` behind a new `evals-adapters` extra, existing `embed_batch`/`Store` seams, pytest hermetic (no `datasets` import needed offline).

## Global Constraints

- `delapan/` is read-only. Adapter changes live in `evals/` + `tests/evals/` + `pyproject.toml` (extra) + `README.md` (docs task).
- Direct insertion only: `store.insert_findings`, never `resolve_and_persist` (the resolver would merge near-duplicate benchmark docs and corrupt gold mapping).
- Chunk size ~1,000 chars (renderer truncates finding content at 1,200); no mid-word cuts; paragraph-boundary preferred.
- Deterministic finding ids: `hashlib.sha1(f"{doc_id}#{i}".encode()).hexdigest()[:32]`.
- Dataset revision pinned at build time (`HfApi().dataset_info(name).sha`) and recorded in the lockfile.
- Gold-doc-missing questions are DROPPED and counted (`dropped_questions`); evidence-narrowing fallbacks counted (`gold_fallback_questions`). Never silently kept/partial.
- Embed-before-insert: all embeddings must succeed before any row is inserted (a partially embedded corpus silently skews retrieval).
- MultiHop-RAG `question_type` containing `"null"` → our `type: unanswerable`, empty `gold_finding_ids`, empty `reference_answer`.
- `oracle_budget: int | None = None` — `None` keeps current behavior byte-identical; value recorded in the run manifest as `"oracle_budget"`.
- House style: `from __future__ import annotations`, full type hints (including `store: Store`), terse module docstring with ASCII flow diagram, lines ≤100 chars (ruff config does not enforce E501 — check manually).
- Hermetic suite passes offline WITHOUT `datasets` installed; `.venv/bin/pytest tests/evals -q && .venv/bin/ruff check evals tests/evals` green after every task.
- Full-suite failure baseline: 2 known pre-existing env failures (`test_build_gateway_slug_without_key_raises_actionable`, `test_settings_boot_without_supabase`); count must not grow.
- Verified dataset schemas (HF datasets-server, 2026-07-26):
  - `ibm-research/watsonxDocsQA` — config `corpus` (split train): `doc_id, url, title, document, md_document, html_document`; config `question_answers` (splits train+test): `question_id, question, correct_answer, correct_answer_document_ids, ground_truths_contexts`. `correct_answer_document_ids` is a string, possibly comma-separated.
  - `yixuantt/MultiHopRAG` — config `MultiHopRAG` (split train): `query, evidence_list (list of {author, category, fact, published_at, source, title, url}), question_type, answer`; config `corpus` (split train): `body, source, url, author, category, published_at, title`. Doc identity = `url`.

## File Structure

```
evals/adapters/
  __init__.py           # empty
  common.py             # Chunk dataclass, chunk_doc, gold_chunk_ids, load_hf,
                        # insert_chunks, write_set_yaml, write_lockfile
  watsonx_docsqa.py     # build() + __main__ CLI
  multihop_rag.py       # build() + __main__ CLI (stratified seeded sampling)
tests/evals/
  test_adapter_common.py
  test_adapter_watsonx.py
  test_adapter_multihop.py
  test_adapter_smoke_live.py   # opt-in RUN_EVALS_ADAPTER_SMOKE=1
evals/arms.py           # oracle_budget param (Task 1)
evals/runner.py         # oracle_budget passthrough + manifest key (Task 1)
evals/__main__.py       # --oracle-budget flag (Task 1)
pyproject.toml          # evals-adapters extra (Task 2)
```

---

### Task 1: `oracle_budget` override through arms, runner, CLI

**Files:**
- Modify: `evals/arms.py` (`build_context` signature + oracle branch)
- Modify: `evals/runner.py` (`run_eval` signature, `_one_record` passthrough, manifest key)
- Modify: `evals/__main__.py` (`--oracle-budget` on `run`)
- Test: `tests/evals/test_arms.py` (append), `tests/evals/test_runner.py` (append)

**Interfaces:**
- Consumes: existing `build_context(arm, *, store, kb_id, question, depth, full_context_cap)`, `_render_rows(rows, budget=None)`, `run_eval(...)`.
- Produces: `build_context(..., oracle_budget: int | None = None)`; `run_eval(..., oracle_budget: int | None = None)`; manifest key `"oracle_budget": oracle_budget` (null when unset); CLI `--oracle-budget INT` (default None).

- [ ] **Step 1: Write the failing tests**

Append to `tests/evals/test_arms.py`:

```python
async def test_oracle_budget_override_prevents_clipping(store):
    org, pid = store.resolve_project("evals-arms-ob", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    # 12 gold chunks x ~900 chars comfortably exceed the 7000-char default budget
    rows = [
        {"id": f"g{i:02d}", "org_id": org, "kb_id": kb, "title": f"Gold chunk {i}",
         "content": f"chunk {i} " + ("x" * 880), "category": "fact", "confidence": 0.5,
         "tags": [], "provenance": [], "embedding": [float(i == j) for j in range(1536)]}
        for i in range(12)
    ]
    await store.insert_findings(rows)
    q = Question(id="qb", question="q?", reference_answer="r",
                 gold_finding_ids=[f"g{i:02d}" for i in range(12)])

    clipped = await build_context("oracle", store=store, kb_id=kb, question=q)
    assert len(clipped.injected_ids) < 12          # default budget clips

    full = await build_context("oracle", store=store, kb_id=kb, question=q,
                               oracle_budget=60_000)
    assert full.injected_ids == [f"g{i:02d}" for i in range(12)]
```

Append to `tests/evals/test_runner.py` (inside the existing module, reusing `seeded` + `SET_YAML`):

```python
async def test_oracle_budget_recorded_in_manifest(seeded, tmp_path):
    project, kb = seeded
    set_path = tmp_path / "set.yaml"
    set_path.write_text(SET_YAML)
    run_dir = await run_eval(
        set_path=set_path, project=project, kb=kb, arms=["oracle"],
        answer_model="m", judge_model="j", out_dir=tmp_path / "runs",
        oracle_budget=24_000,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["oracle_budget"] == 24_000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/evals/test_arms.py::test_oracle_budget_override_prevents_clipping tests/evals/test_runner.py::test_oracle_budget_recorded_in_manifest -v`
Expected: FAIL — `build_context() got an unexpected keyword argument 'oracle_budget'` / `run_eval() got an unexpected keyword argument`

- [ ] **Step 3: Implement**

In `evals/arms.py`, change the signature and oracle branch (only these lines):

```python
async def build_context(
    arm: str,
    *,
    store: Store,
    kb_id: str,
    question: Question,
    depth: str = "normal",
    full_context_cap: int = 60_000,
    oracle_budget: int | None = None,
) -> ArmContext:
```

```python
    if arm == "oracle":
        rows = [store.get_finding(kb_id, fid) for fid in question.gold_finding_ids]
        xml = _render_rows(rows, budget=oracle_budget)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))
```

(`_render_rows(rows, budget=None)` already treats `None` as "default tiers budget" — no change there.)

In `evals/runner.py`: add `oracle_budget: int | None = None` to `run_eval`'s keyword params and to `_one_record`'s params; `_one_record` passes it to `build_context(arm, store=store, kb_id=kb_id, question=q, depth=depth, oracle_budget=oracle_budget)`; `run_eval` passes it into `_one_record` and adds `"oracle_budget": oracle_budget,` to the manifest dict (next to `"depth"`).

In `evals/__main__.py`: `run_p.add_argument("--oracle-budget", type=int, default=None)` and pass `oracle_budget=args.oracle_budget` in the `run` branch's `run_eval` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/evals -q && .venv/bin/ruff check evals tests/evals`
Expected: all pass (47+), ruff clean

- [ ] **Step 5: Commit**

```bash
git add evals/arms.py evals/runner.py evals/__main__.py tests/evals/test_arms.py tests/evals/test_runner.py
git commit -m "feat(evals): oracle_budget override through arms, runner, CLI"
```

---

### Task 2: Adapter common — chunker, gold mapping, writers (pure) + `load_hf`/`insert_chunks` (IO)

**Files:**
- Create: `evals/adapters/__init__.py` (empty), `evals/adapters/common.py`
- Modify: `pyproject.toml` (add `evals-adapters = ["datasets>=3.0"]` after the `evals-hhem` line)
- Test: `tests/evals/test_adapter_common.py`

**Interfaces:**
- Consumes: `delapan.core.clients.embeddings.embed_batch`, `delapan.store.Store`, `delapan.mcp.tenancy.resolve_tenant`, `evals.models.load_question_set`.
- Produces (later tasks rely on exactly these):
  - `@dataclass(frozen=True) Chunk: doc_id: str; doc_title: str; index: int; total: int; text: str; finding_id: str`
  - `chunk_doc(doc_id: str, title: str, text: str, max_chars: int = 1000) -> list[Chunk]`
  - `gold_chunk_ids(chunks: list[Chunk], gold_doc_ids: list[str], evidence_texts: list[str] | None = None) -> tuple[list[str], bool]` — returns `(finding_ids, used_fallback)`; empty list when no gold doc has chunks.
  - `load_hf(name: str, config: str, split: str, revision: str | None = None) -> tuple[list[dict], str]` — returns `(rows, resolved_revision_sha)`; ImportError names the `evals-adapters` extra.
  - `async insert_chunks(store: Store, *, org_id: str, kb_id: str, chunks: list[Chunk], dataset: str, batch_size: int = 128) -> int` — embeds ALL batches first, inserts only after every embedding succeeded; returns count.
  - `write_set_yaml(path: Path, name: str, questions: list[dict], header: str) -> None`
  - `write_lockfile(path: Path, lock: dict) -> None` (json, indent 2, sort_keys)

- [ ] **Step 1: Write the failing test**

```python
# tests/evals/test_adapter_common.py
"""Chunker bounds, deterministic ids, gold narrowing/fallback, yaml round-trip."""
from __future__ import annotations

import hashlib

import pytest

from evals.adapters.common import Chunk, chunk_doc, gold_chunk_ids, write_set_yaml
from evals.models import load_question_set

PARA = "Alpha beta gamma delta. " * 20  # ~480 chars
DOC = "\n\n".join([PARA, PARA, PARA, PARA])  # ~1.9k chars, 4 paragraphs


def test_chunk_doc_bounds_and_ids():
    chunks = chunk_doc("doc-1", "Title", DOC, max_chars=1000)
    assert len(chunks) >= 2
    for i, c in enumerate(chunks):
        assert len(c.text) <= 1000
        assert not c.text[-1:].isspace() and c.text  # no empty/whitespace-tail chunks
        assert c.index == i and c.total == len(chunks)
        assert c.finding_id == hashlib.sha1(f"doc-1#{i}".encode()).hexdigest()[:32]
        assert c.doc_title == "Title"
    # no mid-word cuts: each boundary falls on whitespace in the source
    joined = " ".join(c.text for c in chunks).split()
    assert joined == DOC.split()


def test_chunk_doc_short_passthrough():
    chunks = chunk_doc("d", "T", "short doc")
    assert len(chunks) == 1 and chunks[0].text == "short doc" and chunks[0].total == 1


def test_gold_all_doc_chunks_without_evidence():
    chunks = chunk_doc("d1", "T", DOC, max_chars=1000) + chunk_doc("d2", "T2", DOC, max_chars=1000)
    ids, fallback = gold_chunk_ids(chunks, ["d1"])
    assert ids == [c.finding_id for c in chunks if c.doc_id == "d1"]
    assert fallback is False


def test_gold_narrows_to_evidence_chunk():
    text = ("padding one. " * 60) + "\n\nThe secret launch happened in March.\n\n" + ("padding two. " * 60)
    chunks = chunk_doc("d1", "T", text, max_chars=700)
    ids, fallback = gold_chunk_ids(chunks, ["d1"], evidence_texts=["the SECRET launch  happened in March"])
    assert len(ids) == 1 and fallback is False
    hit = next(c for c in chunks if c.finding_id == ids[0])
    assert "secret launch" in hit.text.lower()


def test_gold_evidence_miss_falls_back_to_doc():
    chunks = chunk_doc("d1", "T", DOC, max_chars=1000)
    ids, fallback = gold_chunk_ids(chunks, ["d1"], evidence_texts=["totally absent sentence"])
    assert ids == [c.finding_id for c in chunks] and fallback is True


def test_gold_missing_doc_returns_empty():
    chunks = chunk_doc("d1", "T", DOC, max_chars=1000)
    ids, fallback = gold_chunk_ids(chunks, ["nope"])
    assert ids == [] and fallback is False


def test_write_set_yaml_round_trips(tmp_path):
    qs = [{"id": "q1", "question": "q?", "reference_answer": "a",
           "gold_finding_ids": ["f1"], "type": "single-hop"},
          {"id": "u1", "question": "u?", "reference_answer": "",
           "gold_finding_ids": [], "type": "unanswerable"}]
    p = tmp_path / "s.yaml"
    write_set_yaml(p, "bench-v1", qs, header="# generated for test\n")
    assert p.read_text().startswith("# generated for test")
    name, loaded = load_question_set(p)
    assert name == "bench-v1" and [q.id for q in loaded] == ["q1", "u1"]


def test_load_hf_missing_dep_actionable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_datasets(name, *a, **kw):
        if name.startswith(("datasets", "huggingface_hub")):
            raise ImportError("nope")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_datasets)
    from evals.adapters.common import load_hf

    with pytest.raises(ImportError, match="evals-adapters"):
        load_hf("x/y", "corpus", "train")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/evals/test_adapter_common.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.adapters'`

- [ ] **Step 3: Implement**

`evals/adapters/__init__.py`: empty.

```python
# evals/adapters/common.py
"""Shared benchmark-adapter machinery: chunk docs into findings, map gold.

    dataset rows ──► chunk_doc ──► Chunk(finding_id=sha1(doc#i)) ──► insert_chunks
    gold doc ids + evidence text ──► gold_chunk_ids ──► gold_finding_ids
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from delapan.core.clients.embeddings import embed_batch
from delapan.store import Store


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    doc_title: str
    index: int
    total: int
    text: str
    finding_id: str


def _finding_id(doc_id: str, index: int) -> str:
    return hashlib.sha1(f"{doc_id}#{index}".encode()).hexdigest()[:32]


def chunk_doc(doc_id: str, title: str, text: str, max_chars: int = 1000) -> list[Chunk]:
    """Split on paragraph boundaries first, then whitespace — never mid-word."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            pieces.append(buf)
            buf = ""
        while len(p) > max_chars:  # oversized paragraph: split on whitespace
            cut = p.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            pieces.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        buf = p
    if buf:
        pieces.append(buf)
    if not pieces:
        pieces = [text.strip() or text]
    total = len(pieces)
    return [
        Chunk(doc_id=doc_id, doc_title=title, index=i, total=total, text=t,
              finding_id=_finding_id(doc_id, i))
        for i, t in enumerate(pieces)
    ]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def gold_chunk_ids(
    chunks: list[Chunk],
    gold_doc_ids: list[str],
    evidence_texts: list[str] | None = None,
) -> tuple[list[str], bool]:
    """Finding ids for a question's gold docs; narrowed to evidence-bearing chunks
    when evidence text is given. (ids, used_fallback). Empty ids = gold doc absent."""
    gold_set = set(gold_doc_ids)
    doc_chunks = [c for c in chunks if c.doc_id in gold_set]
    if not doc_chunks:
        return [], False
    if evidence_texts:
        hits = [
            c for c in doc_chunks
            if any(_norm(e) in _norm(c.text) for e in evidence_texts if e.strip())
        ]
        if hits:
            return [c.finding_id for c in hits], False
        return [c.finding_id for c in doc_chunks], True  # counted fallback
    return [c.finding_id for c in doc_chunks], False


def load_hf(
    name: str, config: str, split: str, revision: str | None = None
) -> tuple[list[dict], str]:
    """Load a HF dataset split at a pinned revision. Returns (rows, revision_sha)."""
    try:
        from datasets import load_dataset
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise ImportError(
            'adapters need the optional extra: uv pip install -e ".[evals-adapters]"'
        ) from exc
    sha = revision or HfApi().dataset_info(name).sha
    ds = load_dataset(name, config, split=split, revision=sha)
    return list(ds), sha


async def insert_chunks(
    store: Store, *, org_id: str, kb_id: str, chunks: list[Chunk],
    dataset: str, batch_size: int = 128,
) -> int:
    """Embed EVERYTHING first, insert only after all embeddings succeed —
    a partially embedded corpus would silently skew retrieval."""
    embeddings: list[list[float]] = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        embeddings.extend(await embed_batch([f"{c.doc_title}\n\n{c.text}" for c in batch]))
    rows = [
        {"id": c.finding_id, "org_id": org_id, "kb_id": kb_id,
         "title": f"{c.doc_title} [{c.index + 1}/{c.total}]", "content": c.text,
         "category": "benchmark-doc", "confidence": 0.5, "tags": [],
         "provenance": [{"url": c.doc_id, "query": dataset}], "embedding": e}
        for c, e in zip(chunks, embeddings)
    ]
    await store.insert_findings(rows)
    return len(rows)


def write_set_yaml(path: Path, name: str, questions: list[dict], header: str) -> None:
    body = yaml.safe_dump(
        {"name": name, "questions": questions},
        sort_keys=False, allow_unicode=True, width=100,
    )
    Path(path).write_text(header + body)


def write_lockfile(path: Path, lock: dict) -> None:
    Path(path).write_text(json.dumps(lock, indent=2, sort_keys=True))
```

In `pyproject.toml`, after the `evals-hhem` line add:

```toml
evals-adapters = ["datasets>=3.0"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/evals/test_adapter_common.py -v && .venv/bin/ruff check evals tests/evals`
Expected: 8 PASS, ruff clean (no `datasets` installed — the import-error test fakes it)

- [ ] **Step 5: Commit**

```bash
git add evals/adapters tests/evals/test_adapter_common.py pyproject.toml
git commit -m "feat(evals): adapter common — chunker, gold mapping, hf loader, inserter"
```

---

### Task 3: watsonxDocsQA adapter

**Files:**
- Create: `evals/adapters/watsonx_docsqa.py`
- Test: `tests/evals/test_adapter_watsonx.py`

**Interfaces:**
- Consumes: everything from Task 2 verbatim; `resolve_tenant(project, kb, create=True)`; `delapan.store.get_store`.
- Produces: `async build(*, project: str = "delapan-evals", kb: str = "watsonx-docsqa", revision: str | None = None, max_docs: int | None = None) -> dict` (the lockfile dict; `max_docs` is a smoke-test knob, recorded in the lockfile) and a `python -m evals.adapters.watsonx_docsqa` CLI. Outputs: set yaml `evals/sets/watsonx-docsqa-v1.yaml`, lockfile `evals/adapters/watsonx-docsqa.lock.json`.
- Dataset constants: `DATASET = "ibm-research/watsonxDocsQA"`; corpus config `corpus`/split `train` with fields `doc_id`, `title`, `document`; QA config `question_answers`, splits `train` + `test`, fields `question_id`, `question`, `correct_answer`, `correct_answer_document_ids` (comma-separated string).

- [ ] **Step 1: Write the failing test**

```python
# tests/evals/test_adapter_watsonx.py
"""watsonxDocsQA adapter against fake HF rows + the real hermetic store."""
from __future__ import annotations

import json

import pytest

import evals.adapters.watsonx_docsqa as wx
from evals.models import load_question_set

CORPUS = [
    {"doc_id": "D1", "title": "Doc One", "document": "Alpha facts. " * 120},
    {"doc_id": "D2", "title": "Doc Two", "document": "Beta facts. " * 120},
]
QA = [
    {"question_id": "t1", "question": "What about alpha?", "correct_answer": "Alpha.",
     "correct_answer_document_ids": "D1"},
    {"question_id": "t2", "question": "Alpha and beta?", "correct_answer": "Both.",
     "correct_answer_document_ids": "D1, D2"},
    {"question_id": "t3", "question": "Missing doc?", "correct_answer": "X.",
     "correct_answer_document_ids": "D404"},
]


@pytest.fixture()
def faked(store, monkeypatch, tmp_path):
    def fake_load_hf(name, config, split, revision=None):
        if config == "corpus":
            return list(CORPUS), "deadbeef"
        return list(QA) if split == "test" else [], "deadbeef"

    async def fake_embed_batch(texts):
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr(wx, "load_hf", fake_load_hf)
    monkeypatch.setattr("evals.adapters.common.embed_batch", fake_embed_batch)
    monkeypatch.setattr(wx, "SET_PATH", tmp_path / "watsonx-docsqa-v1.yaml")
    monkeypatch.setattr(wx, "LOCK_PATH", tmp_path / "watsonx-docsqa.lock.json")
    return tmp_path


async def test_build_writes_kb_set_and_lockfile(faked, store):
    lock = await wx.build(project="wx-test", kb="v1")
    assert lock["dataset"] == wx.DATASET and lock["revision"] == "deadbeef"
    assert lock["dropped_questions"] == ["t3"]          # gold doc absent -> dropped
    assert lock["counts"]["questions"] == 2 and lock["counts"]["docs"] == 2

    name, qs = load_question_set(faked / "watsonx-docsqa-v1.yaml")
    assert name == "watsonx-docsqa-v1" and [q.id for q in qs] == ["t1", "t2"]
    q2 = qs[1]
    d1_ids = set(lock["doc_to_finding_ids"]["D1"])
    d2_ids = set(lock["doc_to_finding_ids"]["D2"])
    assert set(q2.gold_finding_ids) == d1_ids | d2_ids  # multi-doc gold, comma-split

    # findings actually landed with deterministic ids
    org, pid = store.resolve_project("wx-test", create=False)
    kb_id = store.resolve_kb(org, pid, "v1", create=False)
    assert store.count_findings(kb_id) == lock["counts"]["chunks"]
    saved = json.loads((faked / "watsonx-docsqa.lock.json").read_text())
    assert saved == lock


async def test_max_docs_smoke_knob(faked, store):
    lock = await wx.build(project="wx-test2", kb="v1", max_docs=1)
    assert lock["counts"]["docs"] == 1 and lock["max_docs"] == 1
    assert lock["dropped_questions"] == ["t2", "t3"]    # D2 now missing too
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/evals/test_adapter_watsonx.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.adapters.watsonx_docsqa`)

- [ ] **Step 3: Implement**

```python
# evals/adapters/watsonx_docsqa.py
"""watsonxDocsQA -> eval KB + question set (gold at doc granularity).

    corpus(doc_id,title,document) ─► chunk_doc ─► insert_chunks ─► KB
    question_answers(train+test) ─► gold_chunk_ids ─► sets/watsonx-docsqa-v1.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from delapan.mcp.tenancy import resolve_tenant
from delapan.store import get_store

from evals.adapters.common import (
    Chunk,
    chunk_doc,
    gold_chunk_ids,
    insert_chunks,
    load_hf,
    write_lockfile,
    write_set_yaml,
)

DATASET = "ibm-research/watsonxDocsQA"
SET_PATH = Path(__file__).parent.parent / "sets" / "watsonx-docsqa-v1.yaml"
LOCK_PATH = Path(__file__).parent / "watsonx-docsqa.lock.json"


async def build(
    *,
    project: str = "delapan-evals",
    kb: str = "watsonx-docsqa",
    revision: str | None = None,
    max_docs: int | None = None,
) -> dict:
    docs, rev = load_hf(DATASET, "corpus", "train", revision=revision)
    docs = sorted(docs, key=lambda d: d["doc_id"])[: max_docs or len(docs)]
    qa_train, _ = load_hf(DATASET, "question_answers", "train", revision=rev)
    qa_test, _ = load_hf(DATASET, "question_answers", "test", revision=rev)
    qa = sorted(qa_train + qa_test, key=lambda r: str(r["question_id"]))

    chunks: list[Chunk] = []
    for d in docs:
        chunks.extend(chunk_doc(str(d["doc_id"]), str(d["title"]), str(d["document"])))

    ctx = resolve_tenant(project, kb, create=True)
    store = get_store()
    n = await insert_chunks(
        store, org_id=ctx.org_id, kb_id=ctx.kb_id, chunks=chunks, dataset=DATASET
    )

    questions: list[dict] = []
    dropped: list[str] = []
    for row in qa:
        gold_docs = [s.strip() for s in str(row["correct_answer_document_ids"]).split(",") if s.strip()]
        ids, _ = gold_chunk_ids(chunks, gold_docs)
        if not ids:
            dropped.append(str(row["question_id"]))
            continue
        questions.append(
            {"id": str(row["question_id"]), "question": str(row["question"]),
             "reference_answer": str(row["correct_answer"]),
             "gold_finding_ids": ids, "type": "single-hop"}
        )

    header = (
        f"# Generated by evals.adapters.watsonx_docsqa — dataset {DATASET}@{rev}\n"
        f"# Gold granularity: doc-level (all chunks of gold docs). Do not hand-edit.\n"
    )
    write_set_yaml(SET_PATH, "watsonx-docsqa-v1", questions, header)

    doc_to_ids: dict[str, list[str]] = {}
    for c in chunks:
        doc_to_ids.setdefault(c.doc_id, []).append(c.finding_id)
    lock = {
        "dataset": DATASET, "revision": rev,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kb": f"{project}/{kb}", "max_docs": max_docs,
        "counts": {"docs": len(docs), "chunks": n, "questions": len(questions)},
        "dropped_questions": dropped, "gold_fallback_questions": [],
        "doc_to_finding_ids": doc_to_ids,
    }
    write_lockfile(LOCK_PATH, lock)
    return lock


def main() -> int:
    p = argparse.ArgumentParser(prog="evals.adapters.watsonx_docsqa")
    p.add_argument("--project", default="delapan-evals")
    p.add_argument("--kb", default="watsonx-docsqa")
    p.add_argument("--revision", default=None)
    p.add_argument("--max-docs", type=int, default=None)
    a = p.parse_args()
    lock = asyncio.run(
        build(project=a.project, kb=a.kb, revision=a.revision, max_docs=a.max_docs)
    )
    print(f"{lock['kb']}: {lock['counts']} dropped={len(lock['dropped_questions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/evals/test_adapter_watsonx.py -v && .venv/bin/ruff check evals tests/evals`
Expected: 2 PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add evals/adapters/watsonx_docsqa.py tests/evals/test_adapter_watsonx.py
git commit -m "feat(evals): watsonxDocsQA adapter (doc-level gold)"
```

---

### Task 4: MultiHop-RAG adapter (stratified sampling, evidence narrowing, null→unanswerable)

**Files:**
- Create: `evals/adapters/multihop_rag.py`
- Test: `tests/evals/test_adapter_multihop.py`

**Interfaces:**
- Consumes: Task 2 verbatim; `resolve_tenant`; `get_store`.
- Produces: `async build(*, project: str = "delapan-evals", kb: str = "multihop-rag", revision: str | None = None, sample: int = 150, seed: int = 0, max_docs: int | None = None) -> dict`; CLI `python -m evals.adapters.multihop_rag --sample 150 --seed 0`. Outputs: `evals/sets/multihop-rag-v1.yaml` (header notes `--oracle-budget 24000`), `evals/adapters/multihop-rag.lock.json`.
- Dataset constants: `DATASET = "yixuantt/MultiHopRAG"`; corpus config `corpus` (fields `url`, `title`, `body`; doc id = `url`); query config `MultiHopRAG` (fields `query`, `answer`, `question_type`, `evidence_list[{url, fact, ...}]`).
- Question ids: `f"mh-{hashlib.sha1(query.encode()).hexdigest()[:12]}"` (stable across runs/samples).
- `question_type` mapping: contains `"null"` → `unanswerable` (empty gold, empty reference); contains `"temporal"` → `temporal`; contains `"comparison"` or `"inference"` → `multi-hop`.
- Stratified seeded sampling: group queries by raw `question_type`, `random.Random(seed).sample` proportionally (at least 1 per non-empty group; remainder to the largest groups), deterministic given (revision, sample, seed).

- [ ] **Step 1: Write the failing test**

```python
# tests/evals/test_adapter_multihop.py
"""MultiHop-RAG adapter: evidence narrowing, null->unanswerable, seeded sampling."""
from __future__ import annotations

import pytest

import evals.adapters.multihop_rag as mh
from evals.models import load_question_set

BODY_A = ("filler alpha. " * 50) + "\n\nThe merger closed on Tuesday.\n\n" + ("more alpha. " * 50)
BODY_B = ("filler beta. " * 50) + "\n\nProfits doubled last quarter.\n\n" + ("more beta. " * 50)
CORPUS = [
    {"url": "https://ex.com/a", "title": "A", "body": BODY_A},
    {"url": "https://ex.com/b", "title": "B", "body": BODY_B},
]
QUERIES = [
    {"query": "What closed and what doubled?", "answer": "Merger; profits.",
     "question_type": "comparison_query",
     "evidence_list": [
         {"url": "https://ex.com/a", "fact": "The merger closed on Tuesday."},
         {"url": "https://ex.com/b", "fact": "Profits doubled last quarter."},
     ]},
    {"query": "When did the merger close?", "answer": "Tuesday.",
     "question_type": "temporal_query",
     "evidence_list": [{"url": "https://ex.com/a", "fact": "The merger closed on Tuesday."}]},
    {"query": "What is unknowable?", "answer": "Insufficient information.",
     "question_type": "null_query", "evidence_list": []},
]


@pytest.fixture()
def faked(store, monkeypatch, tmp_path):
    def fake_load_hf(name, config, split, revision=None):
        return (list(CORPUS), "cafebabe") if config == "corpus" else (list(QUERIES), "cafebabe")

    async def fake_embed_batch(texts):
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr(mh, "load_hf", fake_load_hf)
    monkeypatch.setattr("evals.adapters.common.embed_batch", fake_embed_batch)
    monkeypatch.setattr(mh, "SET_PATH", tmp_path / "multihop-rag-v1.yaml")
    monkeypatch.setattr(mh, "LOCK_PATH", tmp_path / "multihop-rag.lock.json")
    return tmp_path


async def test_build_narrows_gold_and_maps_null(faked, store):
    lock = await mh.build(project="mh-test", kb="v1", sample=3, seed=0)
    name, qs = load_question_set(faked / "multihop-rag-v1.yaml")
    assert name == "multihop-rag-v1" and len(qs) == 3

    by_type = {q.type: q for q in qs}
    assert set(by_type) == {"multi-hop", "temporal", "unanswerable"}
    assert by_type["unanswerable"].gold_finding_ids == []
    assert by_type["unanswerable"].reference_answer == ""
    # comparison question: exactly one evidence chunk per gold doc (narrowed, not whole docs)
    assert len(by_type["multi-hop"].gold_finding_ids) == 2
    assert lock["gold_fallback_questions"] == []
    assert lock["counts"]["questions"] == 3
    assert "--oracle-budget 24000" in (faked / "multihop-rag-v1.yaml").read_text()


async def test_sampling_is_deterministic_and_stratified(faked, store):
    lock1 = await mh.build(project="mh-t2", kb="v1", sample=2, seed=7)
    lock2 = await mh.build(project="mh-t3", kb="v1", sample=2, seed=7)
    assert lock1["sampled_question_ids"] == lock2["sampled_question_ids"]
    assert lock1["sample"] == 2 and lock1["seed"] == 7
    assert len(lock1["sampled_question_ids"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/evals/test_adapter_multihop.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.adapters.multihop_rag`)

- [ ] **Step 3: Implement**

```python
# evals/adapters/multihop_rag.py
"""MultiHop-RAG -> eval KB + question set (evidence-narrowed gold, null->abstain).

    corpus(url,title,body) ─► chunk_doc ─► insert_chunks ─► KB
    queries ─► stratified seeded sample ─► gold_chunk_ids(evidence facts)
            ─► sets/multihop-rag-v1.yaml (+ null_query -> unanswerable)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import time
from collections import defaultdict
from pathlib import Path

from delapan.mcp.tenancy import resolve_tenant
from delapan.store import get_store

from evals.adapters.common import (
    Chunk,
    chunk_doc,
    gold_chunk_ids,
    insert_chunks,
    load_hf,
    write_lockfile,
    write_set_yaml,
)

DATASET = "yixuantt/MultiHopRAG"
SET_PATH = Path(__file__).parent.parent / "sets" / "multihop-rag-v1.yaml"
LOCK_PATH = Path(__file__).parent / "multihop-rag.lock.json"
RECOMMENDED_ORACLE_BUDGET = 24_000


def _qid(query: str) -> str:
    return f"mh-{hashlib.sha1(query.encode()).hexdigest()[:12]}"


def _our_type(question_type: str) -> str:
    t = question_type.lower()
    if "null" in t:
        return "unanswerable"
    if "temporal" in t:
        return "temporal"
    return "multi-hop"


def _stratified_sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Deterministic proportional sample by raw question_type, >=1 per group."""
    if n >= len(rows):
        return sorted(rows, key=lambda r: _qid(r["query"]))
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(r["question_type"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: _qid(r["query"]))
    names = sorted(groups)
    rng = random.Random(seed)
    quota = {k: max(1, round(n * len(groups[k]) / len(rows))) for k in names}
    while sum(quota.values()) > n:  # trim largest first
        k = max(names, key=lambda x: quota[x])
        quota[k] -= 1
    while sum(quota.values()) < n:
        k = max(names, key=lambda x: len(groups[x]) - quota[x])
        quota[k] += 1
    picked: list[dict] = []
    for k in names:
        picked.extend(rng.sample(groups[k], min(quota[k], len(groups[k]))))
    return sorted(picked, key=lambda r: _qid(r["query"]))


async def build(
    *,
    project: str = "delapan-evals",
    kb: str = "multihop-rag",
    revision: str | None = None,
    sample: int = 150,
    seed: int = 0,
    max_docs: int | None = None,
) -> dict:
    docs, rev = load_hf(DATASET, "corpus", "train", revision=revision)
    docs = sorted(docs, key=lambda d: str(d["url"]))[: max_docs or len(docs)]
    queries, _ = load_hf(DATASET, "MultiHopRAG", "train", revision=rev)

    chunks: list[Chunk] = []
    for d in docs:
        chunks.extend(chunk_doc(str(d["url"]), str(d["title"]), str(d["body"])))

    ctx = resolve_tenant(project, kb, create=True)
    store = get_store()
    n_chunks = await insert_chunks(
        store, org_id=ctx.org_id, kb_id=ctx.kb_id, chunks=chunks, dataset=DATASET
    )

    sampled = _stratified_sample(queries, sample, seed)
    questions: list[dict] = []
    dropped: list[str] = []
    fallbacks: list[str] = []
    for row in sampled:
        qid = _qid(str(row["query"]))
        our_type = _our_type(str(row["question_type"]))
        if our_type == "unanswerable":
            questions.append(
                {"id": qid, "question": str(row["query"]), "reference_answer": "",
                 "gold_finding_ids": [], "type": "unanswerable"}
            )
            continue
        evidence = row.get("evidence_list") or []
        gold_docs = sorted({str(e["url"]) for e in evidence})
        facts = [str(e.get("fact", "")) for e in evidence]
        ids, fallback = gold_chunk_ids(chunks, gold_docs, evidence_texts=facts)
        if not ids:
            dropped.append(qid)
            continue
        if fallback:
            fallbacks.append(qid)
        questions.append(
            {"id": qid, "question": str(row["query"]),
             "reference_answer": str(row["answer"]),
             "gold_finding_ids": ids, "type": our_type}
        )

    header = (
        f"# Generated by evals.adapters.multihop_rag — dataset {DATASET}@{rev}\n"
        f"# sample={sample} seed={seed}. Gold narrowed to evidence chunks.\n"
        f"# Recommended run flag: --oracle-budget {RECOMMENDED_ORACLE_BUDGET}\n"
    )
    write_set_yaml(SET_PATH, "multihop-rag-v1", questions, header)

    doc_to_ids: dict[str, list[str]] = {}
    for c in chunks:
        doc_to_ids.setdefault(c.doc_id, []).append(c.finding_id)
    lock = {
        "dataset": DATASET, "revision": rev,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kb": f"{project}/{kb}", "max_docs": max_docs,
        "sample": sample, "seed": seed,
        "sampled_question_ids": [q["id"] for q in questions],
        "counts": {"docs": len(docs), "chunks": n_chunks, "questions": len(questions)},
        "dropped_questions": dropped, "gold_fallback_questions": fallbacks,
        "doc_to_finding_ids": doc_to_ids,
    }
    write_lockfile(LOCK_PATH, lock)
    return lock


def main() -> int:
    p = argparse.ArgumentParser(prog="evals.adapters.multihop_rag")
    p.add_argument("--project", default="delapan-evals")
    p.add_argument("--kb", default="multihop-rag")
    p.add_argument("--revision", default=None)
    p.add_argument("--sample", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-docs", type=int, default=None)
    a = p.parse_args()
    lock = asyncio.run(
        build(project=a.project, kb=a.kb, revision=a.revision,
              sample=a.sample, seed=a.seed, max_docs=a.max_docs)
    )
    print(f"{lock['kb']}: {lock['counts']} dropped={len(lock['dropped_questions'])} "
          f"fallback={len(lock['gold_fallback_questions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/evals/test_adapter_multihop.py -v && .venv/bin/ruff check evals tests/evals`
Expected: 2 PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add evals/adapters/multihop_rag.py tests/evals/test_adapter_multihop.py
git commit -m "feat(evals): MultiHop-RAG adapter (evidence-narrowed gold, null->unanswerable)"
```

---

### Task 5: Opt-in live smoke, docs, full gate

**Files:**
- Create: `tests/evals/test_adapter_smoke_live.py`
- Modify: `README.md` (extend the `evals/` row's description with "benchmark adapters (watsonxDocsQA, MultiHop-RAG)")
- Modify: `.gitignore` (add `evals/adapters/*.lock.json`? NO — lockfiles are committed by design; do NOT ignore them. No .gitignore change.)

**Interfaces:**
- Consumes: both adapters + `run_eval`.

- [ ] **Step 1: Write the opt-in smoke test**

```python
# tests/evals/test_adapter_smoke_live.py
"""Opt-in: real HF download (max_docs-limited) + 3-question run with real keys.

Run: RUN_EVALS_ADAPTER_SMOKE=1 pytest tests/evals/test_adapter_smoke_live.py -v
Needs: evals-adapters extra installed, AI gateway key. Skipped otherwise."""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_EVALS_ADAPTER_SMOKE"),
    reason="adapter live smoke is opt-in (RUN_EVALS_ADAPTER_SMOKE=1)",
)


async def test_watsonx_smoke(store, tmp_path, monkeypatch):
    import evals.adapters.watsonx_docsqa as wx
    from evals.models import load_question_set
    from evals.runner import run_eval

    monkeypatch.setattr(wx, "SET_PATH", tmp_path / "set.yaml")
    monkeypatch.setattr(wx, "LOCK_PATH", tmp_path / "lock.json")
    lock = await wx.build(project="wx-smoke", kb="v1", max_docs=25)
    assert lock["counts"]["chunks"] > 0

    _, qs = load_question_set(tmp_path / "set.yaml")
    if not qs:
        pytest.skip("no question survived the 25-doc corpus slice")
    trimmed = tmp_path / "set3.yaml"
    text = (tmp_path / "set.yaml").read_text()
    # run only the first question: rewrite the set with one entry
    import yaml as _yaml

    spec = _yaml.safe_load(text)
    spec["questions"] = spec["questions"][:1]
    trimmed.write_text(_yaml.safe_dump(spec, sort_keys=False))

    from delapan.core.config import get_config

    model = get_config().canvas.answer_model
    run_dir = await run_eval(
        set_path=trimmed, project="wx-smoke", kb="v1",
        arms=["closed_book", "production", "oracle"],
        answer_model=model, judge_model=model, out_dir=tmp_path / "runs",
        oracle_budget=24_000,
    )
    records = json.loads((run_dir / "records.json").read_text())
    assert len(records) == 3 and all(not r["unscored"] for r in records)
```

- [ ] **Step 2: Verify it is skipped in the hermetic suite**

Run: `.venv/bin/pytest tests/evals -q`
Expected: all pass, `test_adapter_smoke_live` SKIPPED (plus the existing smoke skip)

- [ ] **Step 3: README**

In the `evals/` row of the "What's inside" table, extend the description to end with: `; benchmark adapters for watsonxDocsQA + MultiHop-RAG (python -m evals.adapters.<name>)`.

- [ ] **Step 4: Full gate**

Run: `.venv/bin/pytest && .venv/bin/ruff check evals tests/evals`
Expected: failure count unchanged vs. the 2-failure baseline; evals suite fully green; ruff clean on evals paths (repo-wide ruff drift is a known separate issue)

- [ ] **Step 5: Commit**

```bash
git add tests/evals/test_adapter_smoke_live.py README.md
git commit -m "feat(evals): adapter live smoke + docs"
```

---

### Task 6 (operator, needs keys + `datasets` install — NOT a subagent code task): bring-up runs

- [ ] Install: `uv pip install -e ".[dev,local,evals,evals-adapters]" -p .venv/bin/python`
- [ ] Build both (local tier env: `DELAPAN_BACKEND=local DELAPAN_DB_PATH=$PWD/evals/corpus/eval-corpus.db`):
  ```bash
  python -m evals.adapters.watsonx_docsqa
  python -m evals.adapters.multihop_rag --sample 150 --seed 0
  ```
- [ ] Runs:
  ```bash
  python -m evals run --set evals/sets/watsonx-docsqa-v1.yaml --project delapan-evals --kb watsonx-docsqa --oracle-budget 24000
  python -m evals run --set evals/sets/multihop-rag-v1.yaml --project delapan-evals --kb multihop-rag --oracle-budget 24000
  ```
- [ ] Commit: both set yamls, both lockfiles, both reports copied to `evals/reports/`.

---

## Self-Review (performed at plan-writing time)

1. **Spec coverage:** direct insertion (T2 `insert_chunks`), chunking to 1,000 (T2), provenance gold + evidence narrowing + counted fallback (T2/T4), sidecar lockfiles with `doc_to_finding_ids` (T3/T4), null→unanswerable (T4), no synopsis (nothing builds one — by construction), sampling explicit/seeded/recorded (T4), `datasets` behind `evals-adapters` extra with actionable ImportError (T2), revision pinning via `HfApi().dataset_info().sha` (T2), oracle budget override + manifest + CLI (T1), dropped-question counting (T3/T4), embed-before-insert (T2), hermetic tests without `datasets` (T2-T4 fake `load_hf`), opt-in smoke (T5), success criteria 1-4 map to T6/T6/T2-5/T6.
2. **Placeholder scan:** clean; T5's .gitignore line explicitly resolves to "no change" rather than a dangling TODO.
3. **Type consistency:** `Chunk` fields used identically in T3/T4; `gold_chunk_ids` returns `(list[str], bool)` and both adapters unpack it; `load_hf` returns `(rows, sha)` and both adapters unpack `(x, rev)`; `build_context`/`run_eval` `oracle_budget` names match T1↔T5 smoke. `insert_chunks` keyword-only args match both call sites.
