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


async def test_train_test_duplicate_question_id_deduped(store, monkeypatch, tmp_path):
    """Same question_id in train and test: keep first occurrence, count the dupe."""
    dup_row = {"question_id": "t1", "question": "What about alpha?",
               "correct_answer": "Alpha.", "correct_answer_document_ids": "D1"}

    def fake_load_hf(name, config, split, revision=None):
        if config == "corpus":
            return list(CORPUS), "deadbeef"
        return [dup_row], "deadbeef"  # identical row surfaces in both splits

    async def fake_embed_batch(texts):
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr(wx, "load_hf", fake_load_hf)
    monkeypatch.setattr("evals.adapters.common.embed_batch", fake_embed_batch)
    monkeypatch.setattr(wx, "SET_PATH", tmp_path / "set.yaml")
    monkeypatch.setattr(wx, "LOCK_PATH", tmp_path / "lock.json")

    lock = await wx.build(project="wx-dup", kb="v1")
    assert lock["counts"]["questions"] == 1
    assert lock["duplicate_question_ids"] == ["t1"]
    _, qs = load_question_set(tmp_path / "set.yaml")
    assert [q.id for q in qs] == ["t1"]


async def test_all_questions_dropped_raises(store, monkeypatch, tmp_path):
    """A build where every question's gold doc is absent raises via load_question_set."""
    bad_qa = [{"question_id": "t1", "question": "?", "correct_answer": "?",
               "correct_answer_document_ids": "D404"}]

    def fake_load_hf(name, config, split, revision=None):
        if config == "corpus":
            return list(CORPUS), "deadbeef"
        return (list(bad_qa) if split == "test" else []), "deadbeef"

    async def fake_embed_batch(texts):
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr(wx, "load_hf", fake_load_hf)
    monkeypatch.setattr("evals.adapters.common.embed_batch", fake_embed_batch)
    monkeypatch.setattr(wx, "SET_PATH", tmp_path / "set.yaml")
    monkeypatch.setattr(wx, "LOCK_PATH", tmp_path / "lock.json")

    with pytest.raises(ValueError, match="no questions"):
        await wx.build(project="wx-alldrop", kb="v1")
