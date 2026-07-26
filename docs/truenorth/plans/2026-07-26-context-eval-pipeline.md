# Context-Injection Eval Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An `evals/` harness that measures hallucination reduction, context efficiency, and retrieval quality of delapan's `<preamble>` injection via a paired closed-book / production / oracle ablation, per the approved spec `docs/truenorth/specs/2026-07-26-context-eval-pipeline-design.md`.

**Architecture:** A new top-level `evals/` package (sibling of `delapan/`) consuming the engine strictly through existing seams — `select_preamble` / `render_preamble`, the `Store` protocol, `text_completion` / `structured_completion`. Pure metric/stat functions are tested hermetically; a CLI (`python -m evals`) drives full runs that write reproducible artifacts (manifest + raw per-question records) from which markdown reports render deterministically.

**Vision goals served:** *Grounding preserved end to end* (faithfulness measured against injected, provenance-carrying findings); *Self-correcting memory writes* (`memory.enabled` ablation quantifies dedup's effect); operationalizes the "resolution observably dedupes" acceptance criterion.

**Tech Stack:** Python 3.12, pytest (asyncio_mode=auto), pydantic v2, tiktoken (pinned `o200k_base`), optional HHEM-2.1-Open via transformers, existing AI-Gateway clients.

## Global Constraints

- House style: `from __future__ import annotations`, full type hints, terse module docstring with ASCII flow diagram, ruff line-length 100.
- Run everything from the repo root of `~/projects/delapan` (branch `master`). `pytest && ruff check .` must pass after every task.
- The offline test suite stays hermetic: no network, no cloud creds, SQLite tier only (`store` fixture from `tests/conftest.py`).
- No engine behavior changes: `delapan/` is read-only for this plan except `pyproject.toml` (extras) and `README.md` (docs task).
- Eval runs must not pollute engine telemetry: always call `select_preamble(..., surface=None)` (no `access_events`/backlog writes — this is how the spec's "drain fire-and-forget telemetry" concern is satisfied by construction).
- Config knobs via env only: `DLP_<SECTION>__<FIELD>`, with `get_config.cache_clear()` after every env change (lru_cache gotcha).
- Scorer failures mark records `unscored` — never a silent default score.
- All new deps land in optional extras: `evals = ["pyyaml>=6.0", "tiktoken>=0.8"]`, `evals-hhem = ["transformers>=4.44", "torch>=2.3"]`. Core install stays untouched.
- Every reported rate carries a bootstrap CI; unanswerable questions are always a separate row, never in headline accuracy.

## File Structure

```
evals/
  __init__.py            # empty marker
  __main__.py            # CLI: run / report / sweep (Task 10)
  models.py              # Question, load_question_set (Task 1)
  arms.py                # context builders per arm (Task 5)
  answerer.py            # answer_question via text_completion (Task 6)
  runner.py              # orchestration: questions × arms → records (Task 10)
  stats.py               # mcnemar_exact, bootstrap_ci, paired_table (Task 3)
  artifact.py            # RunManifest, write_run, load_run (Task 9)
  report.py              # render_report: artifact → markdown (Task 9)
  hermetic.py            # golden-set retrieval metrics used by the pytest tier (Task 11)
  scoring/
    __init__.py
    retrieval.py         # P@K, R@K, MRR, NDCG, verdict calibration (Task 2)
    efficiency.py        # token counts, quality-per-token (Task 4)
    correctness.py       # reference-based judge (Task 7)
    faithfulness.py      # HHEM wrapper + empty-context guard (Task 8)
  corpus/
    manifest.yaml        # pinned public source URLs (Task 12)
    build.py             # fetch → extract → resolve_and_persist → lockfile (Task 12)
  sets/
    generate.py          # LLM question drafting + leakage filter (Task 13)
    v1.yaml              # frozen question set (authored via Task 13's manual step)
tests/evals/
  test_models.py, test_retrieval.py, test_stats.py, test_efficiency.py,
  test_arms.py, test_answerer.py, test_correctness.py, test_faithfulness.py,
  test_artifact_report.py, test_runner.py, test_hermetic_golden.py,
  test_corpus_build.py, test_leakage_filter.py, test_smoke_live.py
```

---

### Task 1: Package scaffolding + question-set model/loader

**Files:**
- Create: `evals/__init__.py`, `evals/models.py`, `evals/scoring/__init__.py`
- Modify: `pyproject.toml` (add `evals` extra after line 32, the `dev` extra)
- Test: `tests/evals/__init__.py` (empty), `tests/evals/test_models.py`

**Interfaces:**
- Produces: `Question` (frozen dataclass: `id: str`, `question: str`, `reference_answer: str`, `gold_finding_ids: list[str]`, `type: str`), `QUESTION_TYPES = frozenset({"single-hop","multi-hop","temporal","unanswerable"})`, `load_question_set(path: Path) -> tuple[str, list[Question]]` returning `(set_name, questions)`.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_models.py
"""Question-set loading: schema validation, duplicate ids, unanswerable rules."""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.models import Question, load_question_set

VALID = """\
name: sample
questions:
  - id: q001
    question: What model does delapan use for embeddings?
    reference_answer: google/gemini-embedding-001 at 1536 dimensions.
    gold_finding_ids: [f1, f2]
    type: single-hop
  - id: q002
    question: What is the airspeed velocity of an unladen swallow?
    reference_answer: ""
    gold_finding_ids: []
    type: unanswerable
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "set.yaml"
    p.write_text(text)
    return p


def test_load_valid_set(tmp_path):
    name, qs = load_question_set(_write(tmp_path, VALID))
    assert name == "sample"
    assert [q.id for q in qs] == ["q001", "q002"]
    assert qs[0].gold_finding_ids == ["f1", "f2"]
    assert qs[1].type == "unanswerable"


def test_duplicate_ids_rejected(tmp_path):
    bad = VALID.replace("q002", "q001")
    with pytest.raises(ValueError, match="duplicate"):
        load_question_set(_write(tmp_path, bad))


def test_unknown_type_rejected(tmp_path):
    bad = VALID.replace("single-hop", "essay")
    with pytest.raises(ValueError, match="type"):
        load_question_set(_write(tmp_path, bad))


def test_answerable_requires_gold_ids(tmp_path):
    bad = VALID.replace("gold_finding_ids: [f1, f2]", "gold_finding_ids: []")
    with pytest.raises(ValueError, match="gold_finding_ids"):
        load_question_set(_write(tmp_path, bad))
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals'`

- [x] **Step 3: Write minimal implementation**

`evals/__init__.py`, `evals/scoring/__init__.py`, `tests/evals/__init__.py`: empty files.

```python
# evals/models.py
"""Question-set schema + loader.

    sets/*.yaml ──► load_question_set ──► list[Question] (validated, frozen)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

QUESTION_TYPES = frozenset({"single-hop", "multi-hop", "temporal", "unanswerable"})


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    reference_answer: str
    gold_finding_ids: list[str] = field(default_factory=list)
    type: str = "single-hop"


def load_question_set(path: Path) -> tuple[str, list[Question]]:
    """Parse and validate one question-set yaml. Raises ValueError on any
    schema violation — a bad set must never produce a silently-partial run."""
    spec = yaml.safe_load(Path(path).read_text())
    questions: list[Question] = []
    seen: set[str] = set()
    for raw in spec.get("questions", []):
        q = Question(
            id=str(raw["id"]),
            question=str(raw["question"]),
            reference_answer=str(raw.get("reference_answer", "")),
            gold_finding_ids=[str(x) for x in raw.get("gold_finding_ids", [])],
            type=str(raw.get("type", "single-hop")),
        )
        if q.id in seen:
            raise ValueError(f"duplicate question id: {q.id}")
        seen.add(q.id)
        if q.type not in QUESTION_TYPES:
            raise ValueError(f"unknown question type {q.type!r} on {q.id}")
        if q.type != "unanswerable" and not q.gold_finding_ids:
            raise ValueError(f"{q.id}: answerable questions need gold_finding_ids")
        questions.append(q)
    if not questions:
        raise ValueError(f"{path}: no questions")
    return str(spec["name"]), questions
```

In `pyproject.toml`, inside `[project.optional-dependencies]` after the `dev` line, add:

```toml
evals = ["pyyaml>=6.0", "tiktoken>=0.8"]
evals-hhem = ["transformers>=4.44", "torch>=2.3"]
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv pip install -e ".[dev,local,evals]" && pytest tests/evals/test_models.py -v && ruff check evals tests/evals`
Expected: 4 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals tests/evals pyproject.toml
git commit -m "feat(evals): package scaffolding + question-set loader"
```

---

### Task 2: Retrieval metrics (pure)

**Files:**
- Create: `evals/scoring/retrieval.py`
- Test: `tests/evals/test_retrieval.py`

**Interfaces:**
- Produces: `precision_at_k(retrieved: list[str], gold: set[str], k: int) -> float`, `recall_at_k(...) -> float`, `mrr(retrieved, gold) -> float`, `ndcg_at_k(retrieved, gold, k) -> float` (binary relevance), `verdict_calibration(records: list[dict]) -> dict` where each record has `{"verdict": str, "gold_ids": list[str], "injected_ids": list[str]}` and the result is `{"rich_n", "rich_gold_injected_rate", "gap_n", "gap_false_rate"}`.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_retrieval.py
"""Hand-computed IR metric cases; verdict calibration on synthetic records."""
from __future__ import annotations

import math

from evals.scoring.retrieval import (
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    verdict_calibration,
)


def test_precision_recall_at_k():
    retrieved, gold = ["a", "b", "c", "d"], {"a", "c", "x"}
    assert precision_at_k(retrieved, gold, 2) == 0.5          # a of {a,b}
    assert precision_at_k(retrieved, gold, 4) == 0.5          # a,c of 4
    assert recall_at_k(retrieved, gold, 4) == 2 / 3           # a,c of {a,c,x}
    assert precision_at_k([], gold, 5) == 0.0
    assert recall_at_k(retrieved, set(), 4) == 0.0            # no gold → 0, not div/0


def test_mrr():
    assert mrr(["x", "a", "y"], {"a"}) == 0.5                 # first hit at rank 2
    assert mrr(["x", "y"], {"a"}) == 0.0


def test_ndcg_binary():
    # retrieved: hit, miss, hit → DCG = 1/log2(2) + 1/log2(4) = 1.5
    # ideal (2 golds): 1/log2(2) + 1/log2(3)
    dcg = 1.0 + 1.0 / math.log2(4)
    idcg = 1.0 + 1.0 / math.log2(3)
    assert ndcg_at_k(["a", "b", "c"], {"a", "c"}, 3) == dcg / idcg
    assert ndcg_at_k(["b"], {"a"}, 1) == 0.0


def test_verdict_calibration():
    records = [
        {"verdict": "rich", "gold_ids": ["a"], "injected_ids": ["a", "b"]},   # good rich
        {"verdict": "rich", "gold_ids": ["z"], "injected_ids": ["a", "b"]},   # false rich
        {"verdict": "gap", "gold_ids": ["a"], "injected_ids": []},            # false gap
        {"verdict": "gap", "gold_ids": [], "injected_ids": []},               # honest gap
    ]
    cal = verdict_calibration(records)
    assert cal["rich_n"] == 2 and cal["rich_gold_injected_rate"] == 0.5
    assert cal["gap_n"] == 2 and cal["gap_false_rate"] == 0.5
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_retrieval.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.scoring.retrieval`)

- [x] **Step 3: Write minimal implementation**

```python
# evals/scoring/retrieval.py
"""Pure IR metrics + coverage-verdict calibration.

    retrieved ids × gold ids ──► P@K / R@K / MRR / NDCG
    per-query verdicts        ──► rich/gap calibration rates
"""

from __future__ import annotations

import math


def precision_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    top = retrieved[:k]
    return sum(1 for r in top if r in gold) / k if k else 0.0


def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return sum(1 for r in retrieved[:k] if r in gold) / len(gold)


def mrr(retrieved: list[str], gold: set[str]) -> float:
    for i, r in enumerate(retrieved, start=1):
        if r in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, r in enumerate(retrieved[:k], 1) if r in gold)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def verdict_calibration(records: list[dict]) -> dict:
    """rich_gold_injected_rate: P(all gold ids injected | verdict=rich).
    gap_false_rate: P(gold existed | verdict=gap). The documented false-`rich`
    failure mode is 1 - rich_gold_injected_rate."""
    rich = [r for r in records if r["verdict"] == "rich"]
    gap = [r for r in records if r["verdict"] == "gap"]
    rich_ok = [r for r in rich if set(r["gold_ids"]) <= set(r["injected_ids"])]
    gap_bad = [r for r in gap if r["gold_ids"]]
    return {
        "rich_n": len(rich),
        "rich_gold_injected_rate": len(rich_ok) / len(rich) if rich else 0.0,
        "gap_n": len(gap),
        "gap_false_rate": len(gap_bad) / len(gap) if gap else 0.0,
    }
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_retrieval.py -v && ruff check evals`
Expected: 4 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/scoring/retrieval.py tests/evals/test_retrieval.py
git commit -m "feat(evals): retrieval metrics + verdict calibration"
```

---

### Task 3: Paired statistics (pure)

**Files:**
- Create: `evals/stats.py`
- Test: `tests/evals/test_stats.py`

**Interfaces:**
- Produces: `mcnemar_exact(b: int, c: int) -> float` (two-sided exact binomial p; `b` = arm-A-only correct, `c` = arm-B-only correct), `bootstrap_ci(values: list[float], n_resamples: int = 10_000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]` (percentile CI on the mean), `paired_table(a: list[bool], b: list[bool]) -> tuple[int, int]` returning `(b_count, c_count)`.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_stats.py
"""Hand-computed McNemar + bootstrap sanity properties (fixed seed)."""
from __future__ import annotations

import math

import pytest

from evals.stats import bootstrap_ci, mcnemar_exact, paired_table


def test_paired_table():
    a = [True, True, False, False, True]
    b = [True, False, True, False, False]
    assert paired_table(a, b) == (2, 1)  # a-only: idx 1,4 → 2; b-only: idx 2 → 1


def test_mcnemar_exact_hand_case():
    # b=8, c=2, n=10: p = 2 * P(X <= 2 | Bin(10, .5)) = 2 * (1+10+45)/1024
    expected = 2 * (1 + 10 + 45) / 1024
    assert math.isclose(mcnemar_exact(8, 2), expected)
    assert math.isclose(mcnemar_exact(2, 8), expected)  # symmetric


def test_mcnemar_degenerate():
    assert mcnemar_exact(0, 0) == 1.0        # no discordant pairs → no evidence
    assert mcnemar_exact(5, 5) == pytest.approx(1.0)  # perfectly balanced, clipped


def test_bootstrap_ci_constant():
    lo, hi = bootstrap_ci([0.7] * 50, seed=1)
    assert lo == hi == 0.7


def test_bootstrap_ci_brackets_mean_and_is_deterministic():
    vals = [0.0] * 40 + [1.0] * 60
    ci1 = bootstrap_ci(vals, seed=42)
    ci2 = bootstrap_ci(vals, seed=42)
    assert ci1 == ci2
    assert ci1[0] < 0.6 < ci1[1]
    assert 0.4 < ci1[0] and ci1[1] < 0.8  # sane width for n=100
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.stats'`

- [x] **Step 3: Write minimal implementation**

```python
# evals/stats.py
"""Small-n paired statistics — no scipy/numpy, exact and seeded.

    paired bools ──► paired_table ──► mcnemar_exact ──► p
    per-q scores ──► bootstrap_ci ──► (lo, hi)
"""

from __future__ import annotations

import math
import random


def paired_table(a: list[bool], b: list[bool]) -> tuple[int, int]:
    """Discordant-pair counts for paired outcomes on the same questions."""
    if len(a) != len(b):
        raise ValueError("paired outcomes must have equal length")
    b_count = sum(1 for x, y in zip(a, b) if x and not y)
    c_count = sum(1 for x, y in zip(a, b) if y and not x)
    return b_count, c_count


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial McNemar p-value over discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_ci(
    values: list[float],
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap CI on the mean; seeded for reproducible reports."""
    if not values:
        raise ValueError("bootstrap_ci needs at least one value")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples)
    )
    lo_i = int((alpha / 2) * n_resamples)
    hi_i = min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))
    return means[lo_i], means[hi_i]
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_stats.py -v && ruff check evals`
Expected: 5 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/stats.py tests/evals/test_stats.py
git commit -m "feat(evals): exact McNemar + seeded bootstrap CIs"
```

---

### Task 4: Context-efficiency metrics

**Files:**
- Create: `evals/scoring/efficiency.py`
- Test: `tests/evals/test_efficiency.py`

**Interfaces:**
- Produces: `TOKENIZER = "o200k_base"`, `count_tokens(text: str) -> int`, `efficiency_per_1k(prod_acc: float, closed_acc: float, mean_tokens: float) -> float` (accuracy delta per 1k injected tokens; `0.0` when no tokens injected).

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_efficiency.py
"""Token counting is pinned to o200k_base; efficiency math is hand-checked."""
from __future__ import annotations

import pytest

from evals.scoring.efficiency import TOKENIZER, count_tokens, efficiency_per_1k


def test_tokenizer_pinned():
    assert TOKENIZER == "o200k_base"


def test_count_tokens_deterministic_and_monotonic():
    short, long = count_tokens("hello world"), count_tokens("hello world " * 50)
    assert short == count_tokens("hello world")  # deterministic
    assert 0 < short < long
    assert count_tokens("") == 0


def test_efficiency_per_1k():
    # +20 points of accuracy for a mean 2000 injected tokens → 0.10 per 1k
    assert efficiency_per_1k(0.8, 0.6, 2000.0) == pytest.approx(0.10)
    assert efficiency_per_1k(0.8, 0.6, 0.0) == 0.0  # closed-book arm: no tokens
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_efficiency.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.scoring.efficiency`)

- [x] **Step 3: Write minimal implementation**

```python
# evals/scoring/efficiency.py
"""Context-efficiency accounting: one pinned tokenizer, quality-per-token.

    preamble xml ──► count_tokens (o200k_base) ──► tokens_injected
    arm accuracies ──► efficiency_per_1k ──► Δacc / 1k tokens
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken

# One tokenizer for every run, ever: the metric is only compared across
# arms/runs, so an approximation is fine — changing it breaks comparability.
TOKENIZER = "o200k_base"


@lru_cache(maxsize=1)
def _enc() -> tiktoken.Encoding:
    return tiktoken.get_encoding(TOKENIZER)


def count_tokens(text: str) -> int:
    return len(_enc().encode(text)) if text else 0


def efficiency_per_1k(prod_acc: float, closed_acc: float, mean_tokens: float) -> float:
    """Accuracy gained over closed-book per 1k injected tokens."""
    if mean_tokens <= 0:
        return 0.0
    return (prod_acc - closed_acc) / (mean_tokens / 1000.0)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_efficiency.py -v && ruff check evals`
Expected: 3 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/scoring/efficiency.py tests/evals/test_efficiency.py
git commit -m "feat(evals): pinned-tokenizer context-efficiency metrics"
```

---

### Task 5: Arm context builders

**Files:**
- Create: `evals/arms.py`
- Test: `tests/evals/test_arms.py`

**Interfaces:**
- Consumes: `Question` (Task 1); engine seams `delapan.core.agent.preamble.render_preamble/select_preamble`, `delapan.core.config.TiersConfig/get_config`, `Store` (`get_finding(kb_id, id)`, `list_findings(kb_id, limit=...) -> {"findings": [...]}` — titles only, so content comes from `get_finding`).
- Produces: `ArmContext` dataclass (`xml: str | None`, `coverage: str | None`, `band_counts: dict[int, int] | None`, `injected_ids: list[str]`), and `async build_context(arm: str, *, store, kb_id: str, question: Question, depth: str = "normal", full_context_cap: int = 60_000) -> ArmContext` for `arm ∈ {"closed_book", "production", "oracle", "full_context"}`.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_arms.py
"""Arm builders against the hermetic SQLite store; embeddings monkeypatched."""
from __future__ import annotations

import pytest

from evals.arms import ArmContext, build_context
from evals.models import Question

VEC_A = [1.0] + [0.0] * 1535   # topic A axis
VEC_B = [0.0, 1.0] + [0.0] * 1534


async def _seed(store) -> tuple[str, str]:
    org, pid = store.resolve_project("evals-arms", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [
            {"id": "fa", "org_id": org, "kb_id": kb, "title": "Alpha fact",
             "content": "delapan embeds with gemini-embedding-001.", "category": "fact",
             "confidence": 0.5, "tags": [], "provenance": [], "embedding": VEC_A},
            {"id": "fb", "org_id": org, "kb_id": kb, "title": "Beta fact",
             "content": "the preamble budget is 7000 chars.", "category": "fact",
             "confidence": 0.5, "tags": [], "provenance": [], "embedding": VEC_B},
        ]
    )
    return org, kb

Q = Question(id="q1", question="how does delapan embed?",
             reference_answer="gemini-embedding-001", gold_finding_ids=["fa"])


async def test_closed_book(store):
    _, kb = await _seed(store)
    ctx = await build_context("closed_book", store=store, kb_id=kb, question=Q)
    assert ctx == ArmContext(xml=None, coverage=None, band_counts=None, injected_ids=[])


async def test_oracle_contains_only_gold(store):
    _, kb = await _seed(store)
    ctx = await build_context("oracle", store=store, kb_id=kb, question=Q)
    assert ctx.injected_ids == ["fa"]
    assert "Alpha fact" in ctx.xml and "Beta fact" not in ctx.xml
    assert ctx.coverage is None  # oracle bypasses retrieval — no verdict


async def test_production_uses_real_retrieval(store, monkeypatch):
    _, kb = await _seed(store)

    async def fake_embed(text: str) -> list[float]:
        return VEC_A

    monkeypatch.setattr("delapan.core.agent.preamble.embed_text", fake_embed)
    ctx = await build_context("production", store=store, kb_id=kb, question=Q)
    assert ctx.coverage in {"rich", "sparse", "gap"}
    assert "fa" in ctx.injected_ids and "fb" not in ctx.injected_ids
    assert ctx.band_counts is not None


async def test_full_context_includes_everything(store):
    _, kb = await _seed(store)
    ctx = await build_context("full_context", store=store, kb_id=kb, question=Q)
    assert set(ctx.injected_ids) == {"fa", "fb"}


async def test_unknown_arm_rejected(store):
    _, kb = await _seed(store)
    with pytest.raises(ValueError, match="unknown arm"):
        await build_context("vibes", store=store, kb_id=kb, question=Q)
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_arms.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.arms'`

- [x] **Step 3: Write minimal implementation**

```python
# evals/arms.py
"""Ablation-arm context builders — every arm reuses the real renderer.

    closed_book ──► None
    production  ──► select_preamble (real retrieval; surface=None, no telemetry)
    oracle      ──► gold findings ─► render_preamble (retrieval bypassed)
    full_context ─► all live findings ─► render_preamble (big budget)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from delapan.core.agent.preamble import render_preamble, select_preamble
from delapan.core.config import TiersConfig, get_config

from evals.models import Question

_FINDING_ID = re.compile(r'<finding id="([^"]+)"')


@dataclass(frozen=True)
class ArmContext:
    xml: str | None
    coverage: str | None
    band_counts: dict[int, int] | None
    injected_ids: list[str] = field(default_factory=list)


def _ids_in(xml: str) -> list[str]:
    return _FINDING_ID.findall(xml)


def _render_rows(rows: list[dict], budget: int | None = None) -> str:
    """Render arbitrary finding rows through the real preamble renderer."""
    for r in rows:
        r.setdefault("similarity", 1.0)
    cfg = get_config().tiers
    if budget is not None:
        cfg = TiersConfig(**{**cfg.model_dump(), "preamble_char_budget": budget})
    return render_preamble([], {1: rows, 2: [], 3: []}, depth="shallow", cfg=cfg)


async def build_context(
    arm: str,
    *,
    store,
    kb_id: str,
    question: Question,
    depth: str = "normal",
    full_context_cap: int = 60_000,
) -> ArmContext:
    if arm == "closed_book":
        return ArmContext(xml=None, coverage=None, band_counts=None)

    if arm == "production":
        # surface=None: no access_events/backlog writes — evals never pollute telemetry.
        xml, coverage = await select_preamble(
            question.question, store=store, kb_id=kb_id, depth=depth, surface=None
        )
        ids = _ids_in(xml)
        # band_counts from the rendered xml would be lossy; recompute cheaply:
        # injected ids are what matters downstream, coverage carries the verdict.
        return ArmContext(xml=xml, coverage=coverage, band_counts={1: len(ids)}, injected_ids=ids)

    if arm == "oracle":
        rows = [store.get_finding(kb_id, fid) for fid in question.gold_finding_ids]
        xml = _render_rows(rows)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))

    if arm == "full_context":
        listed = store.list_findings(kb_id, limit=None)["findings"]
        rows = [store.get_finding(kb_id, r["id"]) for r in listed]
        xml = _render_rows(rows, budget=full_context_cap)
        return ArmContext(xml=xml, coverage=None, band_counts=None, injected_ids=_ids_in(xml))

    raise ValueError(f"unknown arm: {arm}")
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_arms.py -v && ruff check evals`
Expected: 5 PASS, ruff clean. If `TiersConfig` is not a pydantic model (no `.model_dump()`), use `dataclasses.replace`-style construction per its actual definition in `delapan/core/config.py` — check that file and keep the override semantics identical.

- [x] **Step 5: Commit**

```bash
git add evals/arms.py tests/evals/test_arms.py
git commit -m "feat(evals): closed-book/production/oracle/full-context arm builders"
```

---

### Task 6: Answerer

**Files:**
- Create: `evals/answerer.py`
- Test: `tests/evals/test_answerer.py`

**Interfaces:**
- Consumes: `delapan.core.clients.ai_gateway.text_completion(*, model, system, user, temperature=0.0, max_tokens=None) -> str`.
- Produces: `ABSTAIN_MARKER = "I cannot answer this from the available information."`, `async answer_question(question: str, context_xml: str | None, *, model: str, max_tokens: int = 800) -> str`, plus module constants `GROUNDED_SYSTEM` and `CLOSED_BOOK_SYSTEM` (frozen prompt text — they go in the run manifest).

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_answerer.py
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_answerer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.answerer'`

- [x] **Step 3: Write minimal implementation**

```python
# evals/answerer.py
"""Minimal eval answerer — mirrors core/canvas/answer.py's grounding shape.

    question + (context xml | None) ──► system prompt ──► text_completion ──► answer
"""

from __future__ import annotations

from delapan.core.clients.ai_gateway import text_completion

ABSTAIN_MARKER = "I cannot answer this from the available information."

GROUNDED_SYSTEM = (
    "Answer the user's question concisely using the knowledge-base context below. "
    "If the context and your own knowledge are insufficient to answer reliably, "
    f'reply exactly: "{ABSTAIN_MARKER}"\n\n'
    "## KB context\n{context}"
)

CLOSED_BOOK_SYSTEM = (
    "Answer the user's question concisely from your own knowledge. "
    "If you cannot answer reliably, "
    f'reply exactly: "{ABSTAIN_MARKER}"'
)


async def answer_question(
    question: str, context_xml: str | None, *, model: str, max_tokens: int = 800
) -> str:
    system = (
        GROUNDED_SYSTEM.format(context=context_xml)
        if context_xml is not None
        else CLOSED_BOOK_SYSTEM
    )
    return await text_completion(
        model=model, system=system, user=question, max_tokens=max_tokens
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_answerer.py -v && ruff check evals`
Expected: 2 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/answerer.py tests/evals/test_answerer.py
git commit -m "feat(evals): grounded/closed-book answerer with abstention rule"
```

---

### Task 7: Correctness judge (reference-based)

**Files:**
- Create: `evals/scoring/correctness.py`
- Test: `tests/evals/test_correctness.py`

**Interfaces:**
- Consumes: `delapan.core.clients.ai_gateway.structured_completion(*, model, response_format, system, user, ...) -> T`; `Question` (Task 1).
- Produces: `class CorrectnessVerdict(BaseModel)` with `verdict: Literal["correct", "incorrect", "abstained"]` and `reasoning: str`; `async judge(question: Question, answer: str, *, judge_model: str) -> CorrectnessVerdict`; `is_correct(question_type: str, verdict: str) -> bool` (pure: unanswerable → correct iff `abstained`; else correct iff `correct`); `JUDGE_SYSTEM` constant (frozen rubric text — goes in the manifest).

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_correctness.py
"""Judge wiring (mocked LLM) + the pure unanswerable scoring rule."""
from __future__ import annotations

from evals.models import Question
from evals.scoring.correctness import CorrectnessVerdict, is_correct, judge

Q = Question(id="q1", question="capital of France?", reference_answer="Paris",
             gold_finding_ids=["f1"])


async def test_judge_passes_reference_and_parses(monkeypatch):
    captured: dict = {}

    async def fake_structured(*, model, response_format, system, user, **kw):
        captured.update(model=model, user=user)
        return CorrectnessVerdict(verdict="correct", reasoning="matches reference")

    monkeypatch.setattr("evals.scoring.correctness.structured_completion", fake_structured)
    v = await judge(Q, "Paris.", judge_model="j1")
    assert v.verdict == "correct"
    assert captured["model"] == "j1"
    assert "Paris" in captured["user"] and "capital of France?" in captured["user"]


def test_is_correct_rules():
    assert is_correct("single-hop", "correct")
    assert not is_correct("single-hop", "abstained")   # abstaining on answerable ≠ correct
    assert is_correct("unanswerable", "abstained")     # abstention is the right answer
    assert not is_correct("unanswerable", "correct")   # confident answer to unanswerable
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_correctness.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.scoring.correctness`)

- [x] **Step 3: Write minimal implementation**

```python
# evals/scoring/correctness.py
"""Reference-based correctness judge — rubric + CoT, structured verdict.

    question + reference + answer ──► structured_completion ──► CorrectnessVerdict
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from delapan.core.clients.ai_gateway import structured_completion

from evals.models import Question

JUDGE_SYSTEM = (
    "You grade an answer against a reference answer. Reason step by step in "
    "`reasoning`, then give `verdict`:\n"
    "- correct: the answer conveys the reference's substance (wording may differ; "
    "extra correct detail is fine).\n"
    "- incorrect: it contradicts the reference, is wrong, or answers something else.\n"
    "- abstained: it declines to answer or says the information is unavailable.\n"
    "Judge substance only — never length, style, or confidence."
)

_USER_TEMPLATE = (
    "Question: {question}\n\nReference answer: {reference}\n\nAnswer to grade: {answer}"
)


class CorrectnessVerdict(BaseModel):
    reasoning: str = Field(description="Step-by-step comparison against the reference")
    verdict: Literal["correct", "incorrect", "abstained"]


async def judge(question: Question, answer: str, *, judge_model: str) -> CorrectnessVerdict:
    return await structured_completion(
        model=judge_model,
        response_format=CorrectnessVerdict,
        system=JUDGE_SYSTEM,
        user=_USER_TEMPLATE.format(
            question=question.question,
            reference=question.reference_answer or "(none — this question is unanswerable)",
            answer=answer,
        ),
    )


def is_correct(question_type: str, verdict: str) -> bool:
    """Unanswerable questions: abstention IS the correct behavior (and only it)."""
    if question_type == "unanswerable":
        return verdict == "abstained"
    return verdict == "correct"
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_correctness.py -v && ruff check evals`
Expected: 2 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/scoring/correctness.py tests/evals/test_correctness.py
git commit -m "feat(evals): reference-based correctness judge + abstention scoring"
```

---

### Task 8: Faithfulness scorer (HHEM) with empty-context guard

**Files:**
- Create: `evals/scoring/faithfulness.py`
- Test: `tests/evals/test_faithfulness.py`

**Interfaces:**
- Produces: `score_faithfulness(context_xml: str | None, answer: str, predictor: Callable[[str, str], float] | None = None) -> float | None` — returns `None` (exempt) when there is no injected findings content (the empty-context ⇒ perfect-faithfulness trap); `load_hhem() -> Callable[[str, str], float]` (lazy transformers import, actionable ImportError message naming the `evals-hhem` extra).

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_faithfulness.py
"""Empty-context exemption guard; predictor injection; missing-dep message."""
from __future__ import annotations

import pytest

from evals.scoring.faithfulness import score_faithfulness

FAKE = lambda premise, hypothesis: 0.42  # noqa: E731 — injected predictor


def test_none_context_exempt():
    assert score_faithfulness(None, "answer", predictor=FAKE) is None


def test_empty_preamble_exempt():
    assert score_faithfulness("<preamble>\n  <empty/>\n</preamble>", "a", predictor=FAKE) is None


def test_synopsis_only_preamble_exempt():
    xml = "<preamble>\n  <synopsis>\n    <entry topic=\"t\">g</entry>\n  </synopsis>\n</preamble>"
    assert score_faithfulness(xml, "a", predictor=FAKE) is None


def test_findings_present_scores():
    xml = (
        "<preamble>\n  <findings>\n"
        '    <finding id="f1" category="fact">\n      <title>T</title>\n'
        "      <content>C</content>\n    </finding>\n  </findings>\n</preamble>"
    )
    assert score_faithfulness(xml, "a", predictor=FAKE) == 0.42


def test_missing_hhem_dep_is_actionable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_transformers(name, *a, **kw):
        if name.startswith("transformers"):
            raise ImportError("nope")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_transformers)
    from evals.scoring.faithfulness import load_hhem

    with pytest.raises(ImportError, match="evals-hhem"):
        load_hhem()
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_faithfulness.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.scoring.faithfulness`)

- [x] **Step 3: Write minimal implementation**

```python
# evals/scoring/faithfulness.py
"""Faithfulness via a local trained detector (HHEM-2.1-Open), guarded.

    context xml + answer ──► guard: findings present? ──► HHEM(premise, hypothesis)
                                        └─ no ──► None (exempt — never score empty context)
"""

from __future__ import annotations

from typing import Callable

Predictor = Callable[[str, str], float]

_HHEM_ID = "vectara/hallucination_evaluation_model"


def score_faithfulness(
    context_xml: str | None, answer: str, predictor: Predictor | None = None
) -> float | None:
    """Consistency score in [0,1] of `answer` against injected findings, or None.

    None means EXEMPT, not perfect: reference-free faithfulness degenerates to
    ~1.0 on empty context, so records with no injected findings must never be
    averaged into the faithfulness rate."""
    if not context_xml or "<finding " not in context_xml:
        return None
    predictor = predictor or load_hhem()
    return float(predictor(context_xml, answer))


def load_hhem() -> Predictor:
    """Lazy-load HHEM-2.1-Open (CPU, <600MB). Import cost paid once per process."""
    try:
        from transformers import AutoModelForSequenceClassification
    except ImportError as exc:  # pragma: no cover - message content tested via fake import
        raise ImportError(
            'HHEM needs the optional extra: uv pip install -e ".[evals-hhem]"'
        ) from exc

    model = AutoModelForSequenceClassification.from_pretrained(_HHEM_ID, trust_remote_code=True)

    def predict(premise: str, hypothesis: str) -> float:
        return float(model.predict([(premise, hypothesis)]).item())

    return predict
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_faithfulness.py -v && ruff check evals`
Expected: 5 PASS, ruff clean (no transformers install needed — guard tests inject a fake predictor)

- [x] **Step 5: Commit**

```bash
git add evals/scoring/faithfulness.py tests/evals/test_faithfulness.py
git commit -m "feat(evals): HHEM faithfulness scorer with empty-context exemption"
```

---

### Task 9: Run artifacts + deterministic report

**Files:**
- Create: `evals/artifact.py`, `evals/report.py`
- Test: `tests/evals/test_artifact_report.py`

**Interfaces:**
- Consumes: `stats.paired_table/mcnemar_exact/bootstrap_ci` (Task 3), `efficiency.efficiency_per_1k` (Task 4), `correctness.is_correct` (Task 7).
- Produces:
  - `artifact.write_run(out_dir: Path, manifest: dict, records: list[dict]) -> Path` — creates `<out_dir>/` with `manifest.json` and `records.json` (sorted keys, indent 2); `artifact.load_run(run_dir: Path) -> tuple[dict, list[dict]]`.
  - Record shape (one per question × arm — produced by Task 10, consumed here):
    `{"question_id": str, "question_type": str, "arm": str, "answer": str|None, "context_xml": str|None, "chars_injected": int, "tokens_injected": int, "coverage": str|None, "injected_ids": list[str], "verdict": str|None, "judge_reasoning": str|None, "faithfulness": float|None, "latency_s": float, "error": str|None, "unscored": bool}`
  - Manifest keys (written by Task 10): `git_sha, created_at, question_set, set_name, arms, answer_model, judge_model, tokenizer, prompts {grounded_system, closed_book_system, judge_system}, config {tiers, search_max_limit, memory_enabled}, corpus_lockfile_sha256, depth`.
  - `report.render_report(manifest: dict, records: list[dict]) -> str` — markdown, fully deterministic (fixed float format `{:.3f}`, sorted arms), containing: per-arm accuracy over answerable questions with bootstrap CI, a separate unanswerable row (abstention rate per arm), McNemar p for production-vs-closed_book, mean tokens injected + efficiency per 1k, mean faithfulness over scored records only, unscored count with a `NON-COMPARABLE` flag when unscored > 10% of records.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_artifact_report.py
"""Artifact round-trip + byte-deterministic report + unscored flag."""
from __future__ import annotations

from evals.artifact import load_run, write_run
from evals.report import render_report

MANIFEST = {
    "git_sha": "abc1234", "created_at": "2026-07-26T00:00:00Z",
    "question_set": "evals/sets/v1.yaml", "set_name": "sample",
    "arms": ["closed_book", "production"], "answer_model": "m", "judge_model": "j",
    "tokenizer": "o200k_base", "prompts": {}, "config": {}, "depth": "normal",
    "corpus_lockfile_sha256": "",
}


def _rec(qid, arm, verdict, qtype="single-hop", tokens=0, faith=None, unscored=False):
    return {
        "question_id": qid, "question_type": qtype, "arm": arm, "answer": "a",
        "context_xml": None, "chars_injected": 0, "tokens_injected": tokens,
        "coverage": None, "injected_ids": [], "verdict": verdict,
        "judge_reasoning": "", "faithfulness": faith, "latency_s": 0.1,
        "error": None, "unscored": unscored,
    }


RECORDS = [
    _rec("q1", "closed_book", "incorrect"),
    _rec("q1", "production", "correct", tokens=900, faith=0.9),
    _rec("q2", "closed_book", "correct"),
    _rec("q2", "production", "correct", tokens=1100, faith=0.8),
    _rec("q3", "closed_book", "correct", qtype="unanswerable"),
    _rec("q3", "production", "abstained", qtype="unanswerable"),
]


def test_round_trip(tmp_path):
    run_dir = write_run(tmp_path / "r1", MANIFEST, RECORDS)
    manifest, records = load_run(run_dir)
    assert manifest == MANIFEST and records == RECORDS


def test_report_deterministic_and_complete(tmp_path):
    r1 = render_report(MANIFEST, RECORDS)
    r2 = render_report(MANIFEST, RECORDS)
    assert r1 == r2                                   # byte-for-byte
    assert "closed_book" in r1 and "production" in r1
    assert "unanswerable" in r1.lower()               # separate row
    assert "McNemar" in r1
    assert "NON-COMPARABLE" not in r1


def test_unscored_flag():
    records = RECORDS + [_rec("q4", "production", None, unscored=True)]
    assert "NON-COMPARABLE" in render_report(MANIFEST, records)  # 1/7 > 10%
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_artifact_report.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.artifact`)

- [x] **Step 3: Write minimal implementation**

```python
# evals/artifact.py
"""Reproducible run artifacts: manifest + raw per-question records.

    write_run ──► <dir>/manifest.json + records.json (sorted keys)
    load_run  ──► (manifest, records)  — reports render from this alone
"""

from __future__ import annotations

import json
from pathlib import Path


def write_run(out_dir: Path, manifest: dict, records: list[dict]) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out_dir / "records.json").write_text(json.dumps(records, indent=2, sort_keys=True))
    return out_dir


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    records = json.loads((run_dir / "records.json").read_text())
    return manifest, records
```

```python
# evals/report.py
"""Markdown report from an artifact — deterministic, CIs on every rate.

    (manifest, records) ──► per-arm tables + McNemar + efficiency ──► markdown
"""

from __future__ import annotations

from collections import defaultdict

from evals.scoring.correctness import is_correct
from evals.scoring.efficiency import efficiency_per_1k
from evals.stats import bootstrap_ci, mcnemar_exact, paired_table


def _fmt(x: float) -> str:
    return f"{x:.3f}"


def render_report(manifest: dict, records: list[dict]) -> str:
    scored = [r for r in records if not r["unscored"]]
    unscored_n = len(records) - len(scored)
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in scored:
        by_arm[r["arm"]].append(r)

    lines = [
        f"# Eval report — {manifest['set_name']}",
        "",
        f"- git: `{manifest['git_sha']}`  answerer: `{manifest['answer_model']}`  "
        f"judge: `{manifest['judge_model']}`  created: {manifest['created_at']}",
        f"- records: {len(records)} ({unscored_n} unscored)",
    ]
    if records and unscored_n / len(records) > 0.10:
        lines.append("- **NON-COMPARABLE: more than 10% of records are unscored.**")

    lines += ["", "## Accuracy (answerable questions)", "",
              "| arm | n | accuracy | 95% CI | mean tokens | eff/1k tok | faithfulness |",
              "|---|---|---|---|---|---|---|"]

    closed_acc = 0.0
    accs: dict[str, float] = {}
    for arm in sorted(by_arm):
        ans = [r for r in by_arm[arm] if r["question_type"] != "unanswerable"]
        if not ans:
            continue
        outcomes = [1.0 if is_correct(r["question_type"], r["verdict"]) else 0.0 for r in ans]
        acc = sum(outcomes) / len(outcomes)
        accs[arm] = acc
        if arm == "closed_book":
            closed_acc = acc
        lo, hi = bootstrap_ci(outcomes)
        mean_tok = sum(r["tokens_injected"] for r in ans) / len(ans)
        faith = [r["faithfulness"] for r in ans if r["faithfulness"] is not None]
        lines.append(
            f"| {arm} | {len(ans)} | {_fmt(acc)} | [{_fmt(lo)}, {_fmt(hi)}] "
            f"| {mean_tok:.0f} | {_fmt(efficiency_per_1k(acc, closed_acc, mean_tok))} "
            f"| {_fmt(sum(faith) / len(faith)) if faith else 'n/a'} |"
        )

    lines += ["", "## Unanswerable questions (abstention rate)", "",
              "| arm | n | abstained |", "|---|---|---|"]
    for arm in sorted(by_arm):
        una = [r for r in by_arm[arm] if r["question_type"] == "unanswerable"]
        if not una:
            continue
        rate = sum(1 for r in una if r["verdict"] == "abstained") / len(una)
        lines.append(f"| {arm} | {len(una)} | {_fmt(rate)} |")

    if {"closed_book", "production"} <= set(by_arm):
        pairs: dict[str, dict[str, bool]] = defaultdict(dict)
        for r in scored:
            if r["arm"] in {"closed_book", "production"} and r["question_type"] != "unanswerable":
                pairs[r["question_id"]][r["arm"]] = is_correct(r["question_type"], r["verdict"])
        complete = [p for p in pairs.values() if len(p) == 2]
        if complete:
            a = [p["production"] for p in complete]
            b = [p["closed_book"] for p in complete]
            b_n, c_n = paired_table(a, b)
            lines += ["", f"## McNemar production vs closed_book: "
                          f"p = {_fmt(mcnemar_exact(b_n, c_n))} "
                          f"(prod-only correct: {b_n}, closed-only correct: {c_n}, "
                          f"paired n = {len(complete)})"]

    return "\n".join(lines) + "\n"
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_artifact_report.py -v && ruff check evals`
Expected: 3 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/artifact.py evals/report.py tests/evals/test_artifact_report.py
git commit -m "feat(evals): reproducible artifacts + deterministic markdown report"
```

---

### Task 10: Runner + CLI

**Files:**
- Create: `evals/runner.py`, `evals/__main__.py`
- Test: `tests/evals/test_runner.py`

**Interfaces:**
- Consumes: everything above — `load_question_set`, `build_context`, `answer_question`, `judge`/`is_correct`, `score_faithfulness`, `count_tokens`/`TOKENIZER`, `write_run`, `render_report`; engine `resolve_tenant(project, kb, create=False)`, `get_config`/`get_config.cache_clear`.
- Produces:
  - `async run_eval(*, set_path: Path, project: str, kb: str, arms: list[str], answer_model: str, judge_model: str, out_dir: Path, depth: str = "normal", use_hhem: bool = False) -> Path` — returns the run directory. Per question × arm: build context → answer (retry once on exception) → judge (retry once) → faithfulness (only if `use_hhem` and context has findings). Any final failure sets `unscored=True`, `error=str(exc)`, never a default score.
  - CLI: `python -m evals run --set S --project P --kb K [--arms a,b,c] [--answer-model M] [--judge-model J] [--depth D] [--out DIR] [--hhem]`; `python -m evals report RUN_DIR`; `python -m evals sweep --budgets 3000,7000,12000 --depths shallow,deep ...` (one `run_eval` per grid point, setting `DLP_TIERS__PREAMBLE_CHAR_BUDGET` env + `get_config.cache_clear()` before each, restoring after).
  - Model defaults when flags omitted: `get_config().canvas.answer_model` for both answerer and judge.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_runner.py
"""Hermetic end-to-end: seeded store + mocked LLM calls → artifact on disk."""
from __future__ import annotations

import json

import pytest

from evals.runner import run_eval
from evals.scoring.correctness import CorrectnessVerdict

VEC = [1.0] + [0.0] * 1535

SET_YAML = """\
name: e2e
questions:
  - id: q1
    question: how does delapan embed?
    reference_answer: gemini-embedding-001
    gold_finding_ids: [fa]
    type: single-hop
  - id: q2
    question: what is the moon made of?
    reference_answer: ""
    gold_finding_ids: []
    type: unanswerable
"""


@pytest.fixture()
async def seeded(store, monkeypatch):
    org, pid = store.resolve_project("evals-e2e", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [{"id": "fa", "org_id": org, "kb_id": kb, "title": "Embedding model",
          "content": "delapan embeds with gemini-embedding-001.", "category": "fact",
          "confidence": 0.5, "tags": [], "provenance": [], "embedding": VEC}]
    )

    async def fake_embed(text: str) -> list[float]:
        return VEC

    async def fake_answer(*, model, system, user, temperature=0.0, max_tokens=None):
        return "gemini-embedding-001" if "embed" in user else "I cannot answer this."

    async def fake_judge(*, model, response_format, system, user, **kw):
        good = "gemini" in user.split("Answer to grade:")[-1]
        return CorrectnessVerdict(
            reasoning="r", verdict="correct" if good else "abstained"
        )

    monkeypatch.setattr("delapan.core.agent.preamble.embed_text", fake_embed)
    monkeypatch.setattr("evals.answerer.text_completion", fake_answer)
    monkeypatch.setattr("evals.scoring.correctness.structured_completion", fake_judge)
    return "evals-e2e", "main"


async def test_run_eval_writes_complete_artifact(seeded, tmp_path):
    project, kb = seeded
    set_path = tmp_path / "set.yaml"
    set_path.write_text(SET_YAML)

    run_dir = await run_eval(
        set_path=set_path, project=project, kb=kb,
        arms=["closed_book", "production", "oracle"],
        answer_model="m", judge_model="j", out_dir=tmp_path / "runs",
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    records = json.loads((run_dir / "records.json").read_text())

    assert manifest["arms"] == ["closed_book", "production", "oracle"]
    assert manifest["tokenizer"] == "o200k_base"
    assert manifest["prompts"]["judge_system"]           # frozen prompts recorded
    assert len(records) == 6                              # 2 questions × 3 arms
    prod = next(r for r in records if r["arm"] == "production" and r["question_id"] == "q1")
    assert prod["tokens_injected"] > 0 and prod["coverage"] in {"rich", "sparse", "gap"}
    assert all(not r["unscored"] for r in records)
    assert (run_dir / "report.md").exists()


async def test_answer_failure_marks_unscored_not_scored(seeded, tmp_path, monkeypatch):
    project, kb = seeded
    set_path = tmp_path / "set.yaml"
    set_path.write_text(SET_YAML)

    calls = {"n": 0}

    async def always_fail(**kw):
        calls["n"] += 1
        raise RuntimeError("gateway down")

    monkeypatch.setattr("evals.answerer.text_completion", always_fail)
    run_dir = await run_eval(
        set_path=set_path, project=project, kb=kb, arms=["closed_book"],
        answer_model="m", judge_model="j", out_dir=tmp_path / "runs",
    )
    records = json.loads((run_dir / "records.json").read_text())
    assert all(r["unscored"] and r["error"] for r in records)
    assert calls["n"] == 4                                # 2 questions × (1 try + 1 retry)
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.runner'`

- [x] **Step 3: Write minimal implementation**

```python
# evals/runner.py
"""Orchestrates questions × arms into a run artifact.

    set.yaml ──► per (question, arm): build_context ─► answer ─► judge ─► faithfulness
                       └─ any final failure ─► unscored record (never default-scored)
    records + manifest ──► write_run ──► report.md
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from delapan.core.config import get_config
from delapan.mcp.tenancy import resolve_tenant

from evals.answerer import CLOSED_BOOK_SYSTEM, GROUNDED_SYSTEM, answer_question
from evals.arms import build_context
from evals.artifact import write_run
from evals.models import Question, load_question_set
from evals.report import render_report
from evals.scoring.correctness import JUDGE_SYSTEM, judge
from evals.scoring.efficiency import TOKENIZER, count_tokens
from evals.scoring.faithfulness import load_hhem, score_faithfulness


async def _retry_once(coro_fn):
    try:
        return await coro_fn()
    except Exception:  # noqa: BLE001 — one retry, then the caller records the error
        return await coro_fn()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — artifact still valid without a sha
        return "unknown"


async def _one_record(
    q: Question, arm: str, *, store, kb_id: str, depth: str,
    answer_model: str, judge_model: str, predictor,
) -> dict:
    rec: dict = {
        "question_id": q.id, "question_type": q.type, "arm": arm, "answer": None,
        "context_xml": None, "chars_injected": 0, "tokens_injected": 0,
        "coverage": None, "injected_ids": [], "verdict": None, "judge_reasoning": None,
        "faithfulness": None, "latency_s": 0.0, "error": None, "unscored": False,
    }
    start = time.monotonic()
    try:
        ctx = await build_context(arm, store=store, kb_id=kb_id, question=q, depth=depth)
        rec.update(
            context_xml=ctx.xml, coverage=ctx.coverage, injected_ids=ctx.injected_ids,
            chars_injected=len(ctx.xml or ""), tokens_injected=count_tokens(ctx.xml or ""),
        )
        answer = await _retry_once(
            lambda: answer_question(q.question, ctx.xml, model=answer_model)
        )
        rec["answer"] = answer
        verdict = await _retry_once(lambda: judge(q, answer, judge_model=judge_model))
        rec.update(verdict=verdict.verdict, judge_reasoning=verdict.reasoning)
        if predictor is not None:
            rec["faithfulness"] = score_faithfulness(ctx.xml, answer, predictor=predictor)
    except Exception as exc:  # noqa: BLE001 — a failed record is unscored, never defaulted
        rec.update(unscored=True, error=str(exc))
    rec["latency_s"] = round(time.monotonic() - start, 3)
    return rec


async def run_eval(
    *,
    set_path: Path,
    project: str,
    kb: str,
    arms: list[str],
    answer_model: str,
    judge_model: str,
    out_dir: Path,
    depth: str = "normal",
    use_hhem: bool = False,
) -> Path:
    set_name, questions = load_question_set(set_path)
    ctx = resolve_tenant(project, kb, create=False)
    from delapan.store import get_store

    store = get_store()
    predictor = load_hhem() if use_hhem else None

    records = [
        await _one_record(
            q, arm, store=store, kb_id=ctx.kb_id, depth=depth,
            answer_model=answer_model, judge_model=judge_model, predictor=predictor,
        )
        for q in questions
        for arm in arms
    ]

    cfg = get_config()
    manifest = {
        "git_sha": _git_sha(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "question_set": str(set_path), "set_name": set_name, "arms": list(arms),
        "answer_model": answer_model, "judge_model": judge_model,
        "tokenizer": TOKENIZER, "depth": depth,
        "prompts": {
            "grounded_system": GROUNDED_SYSTEM,
            "closed_book_system": CLOSED_BOOK_SYSTEM,
            "judge_system": JUDGE_SYSTEM,
        },
        "config": {
            "tiers": cfg.tiers.model_dump(),
            "search_max_limit": cfg.search.max_limit,
            "memory_enabled": cfg.memory.enabled,
        },
        "corpus_lockfile_sha256": _lockfile_sha(),
    }
    run_dir = write_run(
        Path(out_dir) / time.strftime("%Y%m%d-%H%M%S", time.gmtime()), manifest, records
    )
    (run_dir / "report.md").write_text(render_report(manifest, records))
    return run_dir


def _lockfile_sha() -> str:
    import hashlib

    lock = Path(__file__).parent / "corpus" / "lockfile.json"
    if not lock.exists():
        return ""
    return hashlib.sha256(lock.read_bytes()).hexdigest()
```

```python
# evals/__main__.py
"""CLI: python -m evals {run,report,sweep}.

    run    ──► run_eval ──► evals/runs/<ts>/ (manifest, records, report.md)
    report ──► re-render report.md from an artifact (byte-reproducible)
    sweep  ──► run per (budget × depth) grid point via DLP_ env + cache_clear
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from delapan.core.config import get_config

from evals.artifact import load_run
from evals.report import render_report
from evals.runner import run_eval

DEFAULT_ARMS = ["closed_book", "production", "oracle"]


def _model_default() -> str:
    return get_config().canvas.answer_model


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--set", required=True, type=Path)
    run_p.add_argument("--project", required=True)
    run_p.add_argument("--kb", required=True)
    run_p.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    run_p.add_argument("--answer-model", default=None)
    run_p.add_argument("--judge-model", default=None)
    run_p.add_argument("--depth", default="normal", choices=["shallow", "normal", "deep"])
    run_p.add_argument("--out", type=Path, default=Path("evals/runs"))
    run_p.add_argument("--hhem", action="store_true")

    rep_p = sub.add_parser("report")
    rep_p.add_argument("run_dir", type=Path)

    sw_p = sub.add_parser("sweep")
    sw_p.add_argument("--set", required=True, type=Path)
    sw_p.add_argument("--project", required=True)
    sw_p.add_argument("--kb", required=True)
    sw_p.add_argument("--budgets", default="3000,7000,12000")
    sw_p.add_argument("--depths", default="shallow,deep")
    sw_p.add_argument("--answer-model", default=None)
    sw_p.add_argument("--judge-model", default=None)
    sw_p.add_argument("--out", type=Path, default=Path("evals/runs"))

    args = p.parse_args(argv)

    if args.cmd == "report":
        manifest, records = load_run(args.run_dir)
        print(render_report(manifest, records))
        return 0

    answer_model = args.answer_model or _model_default()
    judge_model = args.judge_model or _model_default()

    if args.cmd == "run":
        run_dir = asyncio.run(
            run_eval(
                set_path=args.set, project=args.project, kb=args.kb,
                arms=args.arms.split(","), answer_model=answer_model,
                judge_model=judge_model, out_dir=args.out, depth=args.depth,
                use_hhem=args.hhem,
            )
        )
        print(run_dir)
        return 0

    # sweep: production arm only — the other arms don't read tiers config.
    for budget in args.budgets.split(","):
        for depth in args.depths.split(","):
            os.environ["DLP_TIERS__PREAMBLE_CHAR_BUDGET"] = budget
            get_config.cache_clear()  # lru_cache — without this every point measures point 1
            run_dir = asyncio.run(
                run_eval(
                    set_path=args.set, project=args.project, kb=args.kb,
                    arms=["closed_book", "production"], answer_model=answer_model,
                    judge_model=judge_model,
                    out_dir=args.out / f"sweep-b{budget}-{depth}", depth=depth,
                )
            )
            print(f"budget={budget} depth={depth} -> {run_dir}")
    os.environ.pop("DLP_TIERS__PREAMBLE_CHAR_BUDGET", None)
    get_config.cache_clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_runner.py -v && ruff check evals`
Expected: 2 PASS, ruff clean. Note: `resolve_tenant` on the local tier ignores auth entirely; the test's `store` fixture env vars make `get_store()` in the runner return the same temp SQLite DB.

- [x] **Step 5: Commit**

```bash
git add evals/runner.py evals/__main__.py tests/evals/test_runner.py
git commit -m "feat(evals): runner + run/report/sweep CLI"
```

---

### Task 11: Hermetic golden-set eval tier

**Files:**
- Create: `evals/hermetic.py`
- Test: `tests/evals/test_hermetic_golden.py`

**Interfaces:**
- Consumes: `tests/golden/*.yaml` + `.vectors.json` sidecars (existing format: `findings` with `id/title/content`, `queries` with `query/expect_verdict/expect_top_ids`), `band_findings`/`assess_coverage` from `delapan.core.agent.preamble`, retrieval metrics (Task 2).
- Produces: `async golden_retrieval_metrics(spec: dict, vectors: dict, store) -> dict` returning `{"per_query": [{"query", "verdict", "expected_verdict", "p_at_3", "mrr", "ndcg_at_5"}], "calibration": <verdict_calibration dict>}` — `expect_top_ids` serve as the gold set.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_hermetic_golden.py
"""The hermetic eval tier: retrieval metrics over the existing golden sets.

Extends tests/test_golden_sets.py from pass/fail assertions to measured
quantities — the documented false-`rich` case becomes a tracked number."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evals.hermetic import golden_retrieval_metrics

GOLDEN = Path(__file__).parent.parent / "golden"


@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*.yaml")), ids=lambda p: p.stem)
async def test_golden_metrics(path, store):
    spec = yaml.safe_load(path.read_text())
    vectors = json.loads(path.with_suffix(".vectors.json").read_text())
    out = await golden_retrieval_metrics(spec, vectors, store)

    assert len(out["per_query"]) == len(spec["queries"])
    for row in out["per_query"]:
        assert 0.0 <= row["mrr"] <= 1.0 and 0.0 <= row["ndcg_at_5"] <= 1.0
        # the golden expectation stays a hard gate, same as test_golden_sets
        assert row["verdict"] == row["expected_verdict"], row["query"]
    cal = out["calibration"]
    assert set(cal) == {"rich_n", "rich_gold_injected_rate", "gap_n", "gap_false_rate"}
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_hermetic_golden.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.hermetic'`

- [x] **Step 3: Write minimal implementation**

```python
# evals/hermetic.py
"""Offline retrieval metrics over golden sets — the pytest eval tier.

    golden yaml + vectors ──► seed store ─► match ─► band ─► IR metrics + calibration
"""

from __future__ import annotations

from delapan.core.agent.preamble import assess_coverage, band_findings
from delapan.core.config import get_config

from evals.scoring.retrieval import mrr, ndcg_at_k, precision_at_k, verdict_calibration


async def golden_retrieval_metrics(spec: dict, vectors: dict, store) -> dict:
    org, pid = store.resolve_project(f"evals-golden-{spec['name']}", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    await store.insert_findings(
        [
            {"id": f["id"], "org_id": org, "kb_id": kb, "title": f["title"],
             "content": f["content"], "category": f.get("category", "fact"),
             "confidence": 0.5, "tags": [], "provenance": [],
             "embedding": vectors[f["id"]]}
            for f in spec["findings"]
        ]
    )
    get_config.cache_clear()
    cfg = get_config().tiers

    per_query: list[dict] = []
    cal_records: list[dict] = []
    for q in spec["queries"]:
        hits = await store.match_findings(
            kb, vectors[f"q:{q['query']}"], match_count=20, min_similarity=cfg.band3_min
        )
        bands = band_findings(hits, cfg)
        verdict = assess_coverage(bands, cfg)
        banded = [r["id"] for b in (1, 2, 3) for r in bands[b]]
        gold = set(q["expect_top_ids"])
        per_query.append(
            {"query": q["query"], "verdict": verdict, "expected_verdict": q["expect_verdict"],
             "p_at_3": precision_at_k(banded, gold, 3), "mrr": mrr(banded, gold),
             "ndcg_at_5": ndcg_at_k(banded, gold, 5)}
        )
        cal_records.append(
            {"verdict": verdict, "gold_ids": list(gold), "injected_ids": banded}
        )
    return {"per_query": per_query, "calibration": verdict_calibration(cal_records)}
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_hermetic_golden.py tests/test_golden_sets.py -v && ruff check evals`
Expected: all PASS (both golden sets × both suites), ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/hermetic.py tests/evals/test_hermetic_golden.py
git commit -m "feat(evals): hermetic golden-set retrieval-metric tier"
```

---

### Task 12: Corpus builder (pinned public sources → eval KB + lockfile)

**Files:**
- Create: `evals/corpus/manifest.yaml`, `evals/corpus/build.py`
- Test: `tests/evals/test_corpus_build.py`

**Interfaces:**
- Consumes: `extract_findings(content, extraction_prompt, source_url, source_query, cfg) -> ExtractionResult` (`delapan/core/exploration/extractor.py:31`), `Finding` model (`delapan/core/exploration/models.py:91` — requires `exploration_id`, `project_id`, `kb`, `category`, `title`, `content: dict`), `resolve_and_persist(ctx, store, candidates, cfg)` (`delapan/core/memory/persist.py:65`), `resolve_tenant(project, kb, create=True)`.
- Produces: `async build_corpus(manifest_path: Path, *, project: str = "delapan-evals", kb: str = "public-v1", fetcher=None) -> dict` (the lockfile dict, also written to `evals/corpus/lockfile.json`): `{"built_at", "manifest_sha256", "sources": [{"url", "content_sha256", "finding_count"}], "finding_ids": [...]}`. `fetcher: Callable[[str], Awaitable[str]] | None` — injectable for tests; default fetches with `httpx.AsyncClient` (already a transitive dep via the openai client).
- Manifest schema: `name`, `domain_note` (one line saying why sources are post-cutoff), `sources: [{url, note}]`.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_corpus_build.py
"""Corpus build with injected fetcher + mocked extraction — no network."""
from __future__ import annotations

import json
from pathlib import Path

from evals.corpus.build import build_corpus

MANIFEST = """\
name: public-v1
domain_note: 2026 AI-tooling release notes — post-cutoff by construction.
sources:
  - url: https://example.com/release-a
    note: project A 2026.07 release notes
"""


async def test_build_corpus_persists_and_locks(tmp_path, store, monkeypatch):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(MANIFEST)

    async def fake_fetch(url: str) -> str:
        return "Project A 2026.07 adds a context-eval harness with three arms."

    async def fake_extract(content, extraction_prompt, source_url, source_query, cfg):
        from delapan.core.exploration.models import ExtractionResult

        return ExtractionResult(
            findings=[{"title": "Project A 2026.07 eval harness",
                       "content": {"fact": "three ablation arms"},
                       "category": "release", "confidence": 0.8}],
            source_url=source_url, source_query=source_query, model_used="fake",
        )

    async def fake_embed_batch(texts):
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr("evals.corpus.build.extract_findings", fake_extract)
    monkeypatch.setattr("delapan.core.memory.persist.embed_batch", fake_embed_batch)
    monkeypatch.setattr(
        "evals.corpus.build.LOCKFILE", tmp_path / "lockfile.json"
    )

    lock = await build_corpus(manifest, project="evals-corpus-t", kb="v1", fetcher=fake_fetch)

    assert lock["sources"][0]["finding_count"] == 1
    assert len(lock["finding_ids"]) == 1
    assert json.loads((tmp_path / "lockfile.json").read_text()) == lock
    # findings actually landed in the store
    kb_row = store.resolve_kb(*store.resolve_project("evals-corpus-t", create=False), "v1",
                              create=False)
    assert store.count_findings(kb_row) == 1
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_corpus_build.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.corpus.build`)

- [x] **Step 3: Write minimal implementation**

`evals/corpus/manifest.yaml` (starter content — final URL list is chosen at the Task 14 manual step, criterion: public, post-knowledge-cutoff, one domain):

```yaml
name: public-v1
domain_note: 2026 AI-tooling release notes and announcements — post-cutoff by construction.
sources: []   # populated at corpus-build time; build.py rejects an empty list
```

```python
# evals/corpus/build.py
"""Deterministic eval-KB builder from pinned public sources.

    manifest.yaml ──► fetch(url) ─► extract_findings ─► Finding ─► resolve_and_persist
                                                   └──► lockfile.json (ids + hashes)
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from delapan.core.config import get_config
from delapan.core.exploration.extractor import extract_findings
from delapan.core.exploration.models import Finding
from delapan.core.memory.persist import resolve_and_persist
from delapan.mcp.tenancy import resolve_tenant

LOCKFILE = Path(__file__).parent / "lockfile.json"

_EXTRACTION_PROMPT = (
    "Extract specific, verifiable facts from this page as findings. Each finding: "
    "a concise title and key-value content of concrete facts (versions, dates, "
    "capabilities, numbers). Skip marketing copy."
)


async def _default_fetch(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def build_corpus(
    manifest_path: Path,
    *,
    project: str = "delapan-evals",
    kb: str = "public-v1",
    fetcher: Callable[[str], Awaitable[str]] | None = None,
) -> dict:
    spec = yaml.safe_load(Path(manifest_path).read_text())
    sources = spec.get("sources") or []
    if not sources:
        raise ValueError(f"{manifest_path}: manifest has no sources")
    fetch = fetcher or _default_fetch

    ctx = resolve_tenant(project, kb, create=True)
    from delapan.store import get_store

    store = get_store()
    cfg = get_config()
    exploration_id = uuid.uuid4().hex

    lock_sources: list[dict] = []
    all_ids: list[str] = []
    for src in sources:
        url = src["url"]
        content = await fetch(url)
        result = await extract_findings(content, _EXTRACTION_PROMPT, url, spec["name"],
                                        cfg.exploration)
        candidates = [
            Finding(
                exploration_id=exploration_id, project_id=ctx.project_id, kb=ctx.kb_id,
                category=raw.get("category") or "fact", title=raw["title"],
                content=raw["content"], confidence=float(raw.get("confidence", 0.7)),
                provenance=[{"url": url, "query": spec["name"]}],
            )
            for raw in result.findings
            if raw.get("title") and raw.get("content")
        ]
        outcome = await resolve_and_persist(ctx, store, candidates, cfg)
        lock_sources.append(
            {"url": url, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
             "finding_count": len(outcome.affected_finding_ids)}
        )
        all_ids.extend(outcome.affected_finding_ids)

    lock = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
        "sources": lock_sources,
        "finding_ids": sorted(all_ids),
    }
    LOCKFILE.write_text(json.dumps(lock, indent=2, sort_keys=True))
    return lock
```

Also create empty `evals/corpus/__init__.py`.

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_corpus_build.py -v && ruff check evals`
Expected: 1 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/corpus tests/evals/test_corpus_build.py
git commit -m "feat(evals): pinned-source corpus builder with lockfile"
```

---

### Task 13: Question-set generator + leakage filter

**Files:**
- Create: `evals/sets/__init__.py` (empty), `evals/sets/generate.py`
- Test: `tests/evals/test_leakage_filter.py`

**Interfaces:**
- Consumes: `structured_completion` (judge-style drafting), `Store.list_findings`/`get_finding`, `Question`/`QUESTION_TYPES` (Task 1).
- Produces: `has_leakage(question: str, source_text: str, max_span: int = 8) -> bool` (pure — True when the question shares a contiguous ≥`max_span`-token span, case-insensitive, with the source); `class DraftQuestion(BaseModel)` (`question`, `reference_answer`, `type`); `async draft_questions(store, kb_id: str, *, model: str, per_finding: int = 1) -> list[dict]` — drafts against each live finding, drops leaking drafts, returns yaml-ready dicts with `gold_finding_ids=[finding_id]`; drafted output is written by the CLI-user to a yaml for **manual curation** before freezing as `evals/sets/v1.yaml`.

- [x] **Step 1: Write the failing test**

```python
# tests/evals/test_leakage_filter.py
"""The lexical-leakage gate: questions must not echo the source span."""
from __future__ import annotations

from evals.sets.generate import has_leakage

SOURCE = (
    "Project A 2026.07 adds a context evaluation harness with three ablation arms "
    "covering closed book, production, and oracle context conditions."
)


def test_verbatim_span_leaks():
    q = "Does the context evaluation harness with three ablation arms covering closed book exist?"
    assert has_leakage(q, SOURCE)                      # ≥8-token contiguous overlap


def test_paraphrase_passes():
    q = "How many experiment conditions does Project A's new eval feature compare?"
    assert not has_leakage(q, SOURCE)


def test_case_insensitive():
    q = "PROJECT A 2026.07 ADDS A CONTEXT EVALUATION HARNESS WITH THREE things?"
    assert has_leakage(q, SOURCE)                      # 8 shared tokens, case-folded


def test_short_overlap_ok():
    q = "What did Project A ship?"
    assert not has_leakage(q, SOURCE)                  # <8-token overlap is fine
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/evals/test_leakage_filter.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `evals.sets.generate`)

- [x] **Step 3: Write minimal implementation**

```python
# evals/sets/generate.py
"""Draft eval questions from findings; reject lexical leakage; human curates.

    findings ──► structured_completion (drafts) ──► has_leakage gate ──► draft yaml
    (a human reviews/edits the draft, then freezes it as sets/v1.yaml)
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from delapan.core.clients.ai_gateway import structured_completion

_DRAFT_SYSTEM = (
    "Write one question a user could ask that this finding answers, plus the "
    "reference answer, USING YOUR OWN WORDS — never copy phrases from the finding. "
    "Prefer questions requiring the finding's specific facts (versions, numbers, dates)."
)


class DraftQuestion(BaseModel):
    question: str
    reference_answer: str
    type: Literal["single-hop", "multi-hop", "temporal"] = "single-hop"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def has_leakage(question: str, source_text: str, max_span: int = 8) -> bool:
    """True when the question contains a contiguous run of >= max_span tokens
    that also appears contiguously in the source — the RAGAS-testset failure
    mode where retrieval scores are inflated because the question echoes the chunk."""
    q, s = _tokens(question), _tokens(source_text)
    if len(q) < max_span:
        return False
    spans = {tuple(s[i : i + max_span]) for i in range(len(s) - max_span + 1)}
    return any(tuple(q[i : i + max_span]) in spans for i in range(len(q) - max_span + 1))


async def draft_questions(
    store, kb_id: str, *, model: str, per_finding: int = 1
) -> list[dict]:
    listed = store.list_findings(kb_id, limit=None)["findings"]
    drafts: list[dict] = []
    for row in listed:
        full = store.get_finding(kb_id, row["id"])
        source = f"{full.get('title', '')}\n{full.get('content', '')}"
        for _ in range(per_finding):
            d = await structured_completion(
                model=model, response_format=DraftQuestion,
                system=_DRAFT_SYSTEM, user=source,
            )
            if has_leakage(d.question, source):
                continue  # drop, don't rephrase — the human curator adds coverage
            drafts.append(
                {"id": f"q-{row['id']}", "question": d.question,
                 "reference_answer": d.reference_answer,
                 "gold_finding_ids": [row["id"]], "type": d.type}
            )
    return drafts
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/evals/test_leakage_filter.py -v && ruff check evals`
Expected: 4 PASS, ruff clean

- [x] **Step 5: Commit**

```bash
git add evals/sets tests/evals/test_leakage_filter.py
git commit -m "feat(evals): question drafting with lexical-leakage gate"
```

---

### Task 14: Live smoke test, docs, and the manual bring-up runbook

**Files:**
- Create: `tests/evals/test_smoke_live.py`
- Modify: `README.md` ("What's inside" table: add an `evals/` row; "Status & roadmap": add the eval pipeline with phase-2 LongMemEval note)
- Modify: `docs/truenorth/plans/2026-07-26-context-eval-pipeline.md` (tick checkboxes as executed)

**Interfaces:**
- Consumes: the full pipeline (Tasks 1–13).

- [x] **Step 1: Write the opt-in live smoke test**

```python
# tests/evals/test_smoke_live.py
"""Opt-in end-to-end smoke: 2 questions × 2 arms with real keys.

Run: RUN_EVALS_SMOKE=1 pytest tests/evals/test_smoke_live.py -v
Skipped in the normal hermetic suite."""
from __future__ import annotations

import json
import os

import pytest

from evals.runner import run_eval

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_EVALS_SMOKE"), reason="live smoke is opt-in (RUN_EVALS_SMOKE=1)"
)

SET_YAML = """\
name: smoke
questions:
  - id: s1
    question: What embedding model does the delapan engine use?
    reference_answer: google/gemini-embedding-001 (1536 dimensions).
    gold_finding_ids: [SMOKE_FINDING_ID]
    type: single-hop
  - id: s2
    question: What is the capital of the moon?
    reference_answer: ""
    gold_finding_ids: []
    type: unanswerable
"""


async def test_live_smoke(store, tmp_path):
    org, pid = store.resolve_project("evals-smoke", create=True)
    kb = store.resolve_kb(org, pid, "main", create=True)
    from delapan.core.clients.embeddings import embed_text

    vec = await embed_text("delapan embeds with google/gemini-embedding-001, 1536 dims")
    await store.insert_findings(
        [{"id": "smoke1", "org_id": org, "kb_id": kb, "title": "Embedding model",
          "content": "The engine embeds with google/gemini-embedding-001 at 1536 dims.",
          "category": "fact", "confidence": 0.5, "tags": [], "provenance": [],
          "embedding": vec}]
    )
    set_path = tmp_path / "smoke.yaml"
    set_path.write_text(SET_YAML.replace("SMOKE_FINDING_ID", "smoke1"))

    from delapan.core.config import get_config

    model = get_config().canvas.answer_model
    run_dir = await run_eval(
        set_path=set_path, project="evals-smoke", kb="main",
        arms=["closed_book", "production"], answer_model=model, judge_model=model,
        out_dir=tmp_path / "runs",
    )
    records = json.loads((run_dir / "records.json").read_text())
    assert len(records) == 4
    assert sum(1 for r in records if r["unscored"]) == 0
```

- [x] **Step 2: Run the hermetic suite to confirm the smoke test is skipped**

Run: `pytest tests/evals -v`
Expected: `test_smoke_live` SKIPPED, everything else PASS

- [x] **Step 3: Update README**

In the "What's inside" table add:

```markdown
| `evals/` | Context-injection eval harness: closed-book/production/oracle ablation, HHEM faithfulness, retrieval + verdict-calibration metrics, paired stats, reproducible run artifacts (`python -m evals run`) |
```

In "Status & roadmap" add a line:

```markdown
- **Eval pipeline** — v1 ablation harness landed (spec: docs/truenorth/specs/2026-07-26-context-eval-pipeline-design.md); phase 2: LongMemEval adapter for externally comparable numbers.
```

- [x] **Step 4: Run the full gate**

Run: `pytest && ruff check .`
Expected: full suite green (the 2 known pre-existing env-related failures on clean master are the only allowed failures — count failures before and after; the count must not grow), ruff clean

- [x] **Step 5: Commit**

```bash
git add tests/evals/test_smoke_live.py README.md
git commit -m "feat(evals): live smoke test + docs"
```

- [x] **Step 6 (manual bring-up — operator, needs keys; not CI):**

```bash
# 1. Pick 8-15 public post-cutoff URLs (2026 AI-tooling release notes), add to
#    evals/corpus/manifest.yaml, then build the corpus KB:
python -c "import asyncio; from pathlib import Path; from evals.corpus.build import build_corpus; print(asyncio.run(build_corpus(Path('evals/corpus/manifest.yaml'))))"
# 2. Draft questions, curate by hand (target 50-150 incl. ~10% unanswerable), freeze:
#    drafts -> evals/sets/v1.yaml  (curation is a human step, by design)
# 3. First real run:
python -m evals run --set evals/sets/v1.yaml --project delapan-evals --kb public-v1
# 4. Commit manifest.yaml, lockfile.json, sets/v1.yaml and the first run's report.
```

---

## Self-Review (performed at plan-writing time)

1. **Spec coverage:** hermetic tier (T11), full tier + CLI (T10), corpus from pinned public post-cutoff sources (T12), question set with unanswerable handling (T1, T13, T14 manual), four arms (T5), pinned answerer prompts (T6), reference-based judge (T7), HHEM + empty-context guard (T8), retrieval + verdict calibration (T2, T11), efficiency + pinned tokenizer (T4), McNemar + bootstrap (T3), artifacts/report/reproducibility incl. NON-COMPARABLE flag (T9, T10), sweep mechanism with cache_clear (T10), telemetry non-pollution via `surface=None` (T5), success criteria 1–6 map to T10/T9/T11/T10-sweep/T9/T14 respectively. Judge-panel periodic validation is intentionally manual/process (spec: "periodic, sampled") — no code task needed.
2. **Placeholder scan:** none — every step has complete code/commands. The empty `sources: []` in T12's manifest is a validated-against runtime state, not a plan placeholder; T14 step 6 is the operator runbook that fills it.
3. **Type consistency:** record dict shape defined in T9 matches what T10's `_one_record` emits; `ArmContext` fields (T5) match runner usage (T10); `CorrectnessVerdict` (T7) is imported by T10's test; `verdict_calibration` input shape (T2) matches T11's `cal_records`. One flagged uncertainty is handled in-plan: T5 Step 4 notes the `TiersConfig.model_dump()` fallback if it isn't pydantic.
