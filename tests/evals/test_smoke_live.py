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
