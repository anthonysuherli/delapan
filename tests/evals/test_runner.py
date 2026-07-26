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
