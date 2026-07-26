"""Orchestrates questions × arms into a run artifact.

    set.yaml ──► per (question, arm): build_context ─► answer ─► judge ─► faithfulness
                       └─ any final failure ─► unscored record (never default-scored)
    records + manifest ──► write_run ──► report.md
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from delapan.core.config import get_config
from delapan.mcp.tenancy import resolve_tenant
from delapan.store import Store
from evals.answerer import CLOSED_BOOK_SYSTEM, GROUNDED_SYSTEM, answer_question
from evals.arms import build_context
from evals.artifact import write_run
from evals.models import Question, load_question_set
from evals.report import render_report
from evals.scoring.correctness import JUDGE_SYSTEM, judge
from evals.scoring.efficiency import TOKENIZER, count_tokens
from evals.scoring.faithfulness import Predictor, load_hhem, score_faithfulness

T = TypeVar("T")


async def _retry_once(coro_fn: Callable[[], Awaitable[T]]) -> T:
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
    q: Question, arm: str, *, store: Store, kb_id: str, depth: str,
    answer_model: str, judge_model: str, predictor: Predictor | None,
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
