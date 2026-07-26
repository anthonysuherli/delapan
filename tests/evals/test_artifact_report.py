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
