"""Chunker bounds, deterministic ids, gold narrowing/fallback, yaml round-trip."""
from __future__ import annotations

import hashlib

import pytest

from evals.adapters.common import chunk_doc, gold_chunk_ids, write_set_yaml
from evals.models import load_question_set

PARA = "Alpha beta gamma delta. " * 20  # ~480 chars
DOC = f"{PARA}\n\n{PARA}\n\n{PARA}\n\n{PARA}"  # ~1.9k chars, 4 paragraphs


def test_chunk_doc_bounds_and_ids():
    chunks = chunk_doc("doc-1", "Title", DOC, max_chars=1000)
    assert len(chunks) >= 2
    for i, c in enumerate(chunks):
        assert len(c.text) <= 1000
        assert not c.text[-1:].isspace() and c.text  # no empty/whitespace-tail
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
    chunks = (
        chunk_doc("d1", "T", DOC, max_chars=1000)
        + chunk_doc("d2", "T2", DOC, max_chars=1000)
    )
    ids, fallback = gold_chunk_ids(chunks, ["d1"])
    assert ids == [c.finding_id for c in chunks if c.doc_id == "d1"]
    assert fallback is False


def test_gold_narrows_to_evidence_chunk():
    text = (
        ("padding one. " * 60)
        + "\n\nThe secret launch happened in March.\n\n"
        + ("padding two. " * 60)
    )
    chunks = chunk_doc("d1", "T", text, max_chars=700)
    ids, fallback = gold_chunk_ids(
        chunks, ["d1"], evidence_texts=["the SECRET launch  happened in March"]
    )
    assert len(ids) == 1 and fallback is False
    hit = next(c for c in chunks if c.finding_id == ids[0])
    assert "secret launch" in hit.text.lower()


def test_gold_evidence_miss_falls_back_to_doc():
    chunks = chunk_doc("d1", "T", DOC, max_chars=1000)
    ids, fallback = gold_chunk_ids(chunks, ["d1"], evidence_texts=["totally absent"])
    assert ids == [c.finding_id for c in chunks] and fallback is True


def test_gold_missing_doc_returns_empty():
    chunks = chunk_doc("d1", "T", DOC, max_chars=1000)
    ids, fallback = gold_chunk_ids(chunks, ["nope"])
    assert ids == [] and fallback is False


def test_write_set_yaml_round_trips(tmp_path):
    qs = [
        {
            "id": "q1",
            "question": "q?",
            "reference_answer": "a",
            "gold_finding_ids": ["f1"],
            "type": "single-hop",
        },
        {
            "id": "u1",
            "question": "u?",
            "reference_answer": "",
            "gold_finding_ids": [],
            "type": "unanswerable",
        },
    ]
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


def test_chunk_doc_empty_and_whitespace_yield_no_chunks():
    assert chunk_doc("d", "T", "") == []
    assert chunk_doc("d", "T", "   \n\n  ") == []


def test_chunk_doc_unbroken_run_hard_splits():
    chunks = chunk_doc("d", "T", "x" * 1500, max_chars=1000)
    assert [len(c.text) for c in chunks] == [1000, 500]  # documented exception:
    # a boundary-free token cannot satisfy the no-mid-word rule


def test_chunk_doc_namespace_prevents_collisions():
    """Chunks with namespace have different finding_ids than without."""
    text = "Test document content."
    chunks_no_ns = chunk_doc("d1", "T", text)
    chunks_ns = chunk_doc("d1", "T", text, id_namespace="proj/kb1")
    assert chunks_no_ns[0].finding_id != chunks_ns[0].finding_id
    # The namespaced version should include namespace in the hash
    expected_id_no_ns = hashlib.sha1(b"d1#0").hexdigest()[:32]
    expected_id_ns = hashlib.sha1(b"proj/kb1|d1#0").hexdigest()[:32]
    assert chunks_no_ns[0].finding_id == expected_id_no_ns
    assert chunks_ns[0].finding_id == expected_id_ns
