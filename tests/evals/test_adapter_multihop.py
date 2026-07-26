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

    # Cache inserted chunks to simulate persistence across build() calls
    inserted: dict[str, int] = {}

    async def fake_insert_chunks(store, *, org_id, kb_id, chunks, dataset, **kw):
        key = f"{org_id}/{kb_id}"
        if key not in inserted:
            inserted[key] = len(chunks)
        return len(chunks)

    monkeypatch.setattr(mh, "load_hf", fake_load_hf)
    monkeypatch.setattr("evals.adapters.common.embed_batch", fake_embed_batch)
    monkeypatch.setattr("evals.adapters.multihop_rag.insert_chunks", fake_insert_chunks)
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
